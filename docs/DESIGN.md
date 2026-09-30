# Verinoda design (v0.2 target)

This document turns research findings into design decisions. Each decision
records what it builds on, what measurement it must move, and what stays out of
scope. Findings from the research runs are cited as **[R-intent]**, **[R-refs]**,
**[R-retrieval]**, **[R-precision]**, **[R-verify]** and **[R-profile]**. Their
primary sources are listed at the end.

## Implementation status (2026-09-23)

The table records what the round-3 tracks and the integration step built, as
reported by the track reports and checked against the code. It adds status
only: the decisions below are unchanged. **implemented** = built, wired into
the CLI/MCP/analysis and covered by the product test suite. **partial** = built
with the stated gaps. **not done** = not built. Numbers here are the tracks'
own measurements, with their caveats. The benchmark harness results are in
[BENCHMARKS.md](BENCHMARKS.md).

| # | Decision | Status | Note / deviation from the design |
|---|---|---|---|
| D1 | Question plan contract | implemented | Schema in `verinoda/schemas/question_plan.v1.json`; CLI `plan draft/check/schema/audit` with plan files under `.verinoda/plans/`; MCP `question_plan_draft` / `question_plan_check` / `analyze(plan_json=...)`. |
| D2 | Deterministic validation | implemented | All listed checks. The mention-linking thresholds are constants (`question_plan.THRESHOLDS`); `config.json` `understanding` mirrors them but is not read by the check yet. |
| D3 | Grounding in evidence tiers | implemented | Tiers and statuses as designed. Domain concepts that match many names are always merged, never asked about. |
| D4 | Same path with or without a host plan | implemented | `draft()` adds *domain shadowing*: a Turkish cue that is also a domain word of the repository needs a second cue. The 36/36 intent and segmentation score is on a gold table written by the rule author (in-sample). |
| D5 | Text normalisation | implemented | `verinoda/textnorm.py`, shared by retrieval, lexicon and plans. |
| D6 | Repo-learned lexicon | implemented | Deviations: parameter names are recorded but not associated; units in test files feed the vocabulary but not the association; identical units (e.g. a doc line copied into several files) count once. The seed dictionary has about 325 entries (about 200 planned). Lexicon pairs are sparse on small repositories (orders_app: 1 pair), so Turkish linking there relies mostly on the seed dictionary. |
| D7 | Execution per sub-question | implemented | Intent handlers, budget share per sub-question, `done_when` verdicts; `verinoda plan audit` recomputes them. |
| D8 | Plans are stored | partial | `question_plans` (schema v3) with immutable bodies and `parent_id` revisions; analyses reference their plan. Not done: turning a "you misunderstood me" feedback into a plan revision (`store_plan(..., source="revised")` exists, `feedback.py` does not call it). |
| D9 | Skills: understand the question first | implemented | Claude Code (`AskUserQuestion`) and Codex (`request_user_input` or plain text) skills. `docs/AGENT-VERIFICATION.md` records real headless sessions with the earlier skill text; there is no such record for the round-3 text. |
| 1.3 | Measurements (benchmark schema 2) | partial | The measurement step is adding Turkish paraphrase sets (`orders_app_tr`, `graphify_core_tr`, written by the rule author, so in-sample) and a held-out set to the harness. Intent macro-F1, segmentation, mention-linking and clarification metrics are not in the harness; the 36-question intent gold table lives in `tests/test_question_plan.py`. The understanding track measured in isolation: Turkish fact recall on orders_app 18 → 30 of 32 (English 32), on graphify_core 6 → 12 of 37 (English 19). |
| D10 | `verinoda/references/` pipeline | implemented | Deviation: `reference_resolutions` has the columns id, text, explicit, network, result and created_at. The input hash, status and language are stored inside the `result` JSON. Offline test strategy: `git url.insteadOf` + `GIT_ALLOW_PROTOCOL=file` + `CassetteTransport`; cassettes are recorded with `tools/record_reference_cassettes.py`. |
| D11 | Pin precedence ladder | implemented | `references/pin.py`; a named version never falls back to a floating ref (M10 → unresolved). |
| D12 | Mismatch catalogue M1-M12 | partial | All codes exist. Some fire only narrowly: M7 only from an issue-API 301 (a rename behind git redirects is not seen), M12 only from `ls-remote` not-found/auth, M8 only from research, and M6 only when the URL names a commit that is not an ancestor of the current head. |
| D13 | Local version resolver | implemented | Lock files (uv, pylock, poetry, Pipfile, requirements `==`, package-lock, Cargo.lock, go.mod + go.sum), the project's own venv metadata (no import), runtime pins. |
| D14 | Package → source by strength | partial | Content match is wired only into `research` for PyPI sdists; npm, crates and Go artifacts are not compared. PEP 740 provenance is parsed but not used by `resolve()`, and the Sigstore signature is not verified. PR merge state, force-push timelines and closing commits are not used for pinning. |
| D15 | Offline-first transport | partial | Live / cache / cassette transports, no credentials stored, per-host intervals and rate-limit blocking. Missing: a token bucket, a contact User-Agent from config, and `gh` / `GITHUB_TOKEN` auth. Network blocking is done per test module, not by a suite-wide fixture. |
| D16 | Integration | implemented | `feedback.process` resolves first; `dependency_source` for locked versions; CLI `verinoda resolve`, MCP `reference_resolve`; analysis resolves offline (`network="off"`, `local_intent=True`). |
| 2.3 | Measurements | partial | 56 questions (44 in-sample, 12 held out), target ≥ 60. The held-out 12 scored 11/12 on their blind first run. The miss was fixed afterwards, so that set is no longer blind. |
| D17 | Persistent passage index | implemented | `.verinoda/index/search.db`; the 400-file cap is gone. Files edited after indexing are reported (`budget.stale_files`), not re-indexed at query time. |
| D18 | BM25F ranking | implemented | As designed. A final `e` is dropped after stemming (`save`/`saved`). This was found on an English orders_app check after the held-out run and cost one held-out fact (27 → 26 of 33). 2026-09-23: adjacent question words that are adjacent in a code passage or unit name count 1.3x (`PROX_BOOST`; chosen among four variants with the held-out set in view, see BENCHMARKS.md "Update 2026-09-23"). |
| D19 | Graph prior | implemented | Deviation: push activation is `eps·deg(v)` with re-queueing. The prototype stopped pushing mass to a node once it had been skipped; that bug was fixed. |
| D20 | Plain text for the model | implemented | `retrieval.render_text`; `verinoda query` prints it by default and MCP `project_query` defaults to `format="text"`. The track measured text vs JSON: graphify_core 35/37 vs 27/37 and held-out 26/33 vs 19/33. |
| D21 | Build-time work not repeated | implemented | One-pass spans, receiver-call sidecar, per-file interval index, stat-cached hashes, one freshness pass per analyze passed to critique, and an MCP server that keeps graph, spans and lexicon. Deviation: the path-identity memo is a monkeypatch applied from `index.build()`, not a patch to the vendored file (`docs/UPSTREAM.md`). |
| D22 | Vocabulary gaps | implemented | Held-out h02 ("environment variables") is still 1/4 in text; not tuned, to keep the held-out set clean. |
| 3.2 | Targets | met in the track's measurement | Warm query median 30-34 ms (target < 50 ms), cold CLI 0.59 s (target < 1 s). Facts per token on held-out beat Graphify in text form (26/33 at 1,413 tok/q vs Graphify 17/33 at 1,659), not in JSON (19/33). graphify_core is in-sample. |
| D23 | Symbol facts | implemented | Deviation: non-Python name bindings use one coarse fingerprint (all top-level non-definition statements); there are no per-name tree-sitter binding tables. |
| D24 | Facet-level dependencies | implemented | Relation claims depend on the whole caller body, not the call statement, so relation claims still showed a 21% false-stale rate in the replay. `test_run` claims use file dependencies over the test files' import closure, not coverage. Claims created before schema v3 keep the file rule. |
| D25 | Anchored evidence | implemented | Relocation outcomes as designed, plus `ambiguous` for unanchored lines that occur more than once. |
| D26 | Verdict rules | implemented | Enforced centrally in `claims.check_status` for every stored status change. Interpretation: for `decision` / `history` claims, `primary_source_verified` accepts the partial grade when the record states what the claim attributes to it. Otherwise no ADR or history answer could ever be primary-source verified. |
| D27 | Counter-hypothesis probes | implemented | The labelled critique set (45 claims) is in-sample: probes were added after seeing its misses. The confidence caps were not recalibrated. |
| D28 | Runtime observation | implemented | CLI `observe` and `analyze --observe`, MCP `runtime_observe`. Deviations: overhead is 1.39× CPU (median) on 533 Graphify tests, against the research's 1.23×, because boundary calls are recorded and the trace is written inside the timed window. The `setprofile` fallback costs about 4.5× and was only forced on CPython 3.12. Child processes are not traced. The container path has not been run against a real docker/podman. The observed-edge retrieval channel is not built. |
| D29 | Precise resolution | implemented | `verinoda[precise]` (jedi), `resolve-call`, `scan --precise`, `scan --scip FILE`, MCP `resolve_call`, per-analysis budget. Stricter than the research: a method called on a parameter or local receiver is `dynamic`, never definitive. SCIP is used for non-Python files only. |
| D30 | Measurement harness | implemented | `verinoda benchmark staleness replay\|mutations` and `verinoda benchmark critique-eval`. The replay samples claims whose evidence is in modified files; incoming relations from unchanged files are not sampled. |
| D31 | No laundering (truth rules) | implemented | Python for relation scopes, config bindings, order and location existence; other languages keep their grades, and their config and relation claims stop at `strong_inference` (2026-09-26). Word overlap alone never verifies; `contains:` verifies only the quoted text; written claims naming more than the typed check binds stay partial. In-sample fixtures; no held-out false-sentence set yet. |
| D32 | Name-existence check | partial | Python only (other languages and files that do not parse are listed as not checked, exit 4; 2026-09-26): `verinoda check` (files, `--diff`, `--stdin --as`) and `verinoda api`, MCP `code_check` / `api_members`, skill text. Not done: mod config keys and resource ids, JVM jars, JS/TS, `--against PKG==VER`, the environment fingerprint in snapshots. Measurements in BENCHMARKS.md (the fixture set was written by the rule author: in-sample). |
| D33 | Decisions stay human | partial | Built: the `decide` intent (EN/TR cue tables) with the verdict `human_decision_required`, never `met`; decision records (`verinoda/decisions.py`, schema v5 log, `decide record/import/guard/accept/waive/list`, MCP `decision_record`); guards and `decide check` (`verinoda/guards.py`, MCP `decision_check`, a one-line summary in `update`; critique's exclusivity check and feedback's exclusive corrections use the same engine). Guard mutations (54 cases on the three examples, written by the rule author, plus 22 forms from the two reviews added by the fixer: all in-sample): VIOLATED precision 1.00, recall 1.00 in reach, 13/13 out-of-reach forms named in limits or POSSIBLE; the old raw-regex scan on the same orders_app cases tp 7 fp 8 fn 6. `decide check` median 42 ms per example case, about 2 s (1.8-2.2 s) on the full Verinoda tree (3 guards; results in `benchmarks/results/decide-2026-09-25/`). The decision brief (`verinoda/decision_brief.py`, `decide brief/answer`, MCP `decision_brief`, answers through `decision_record(action='answer')`; `analyze` routes decide sub-questions to it): on orders_app, EN and TR question, 8/8 gold forces, 19/19 cited evidence re-checks, 5/5 gold question kinds - in-sample (the gold came with the design and the probes were written after it). Not built: `analyze` impact questions do not include violations; the UI shows no decision badge; claims for accepted guards (kinds `exclusive` / `layering`); an ADR's reasons are matched by a few phrasings only; `research.dependencies` itself still reads no Gradle/Maven (the guards and the brief read them). Intent routing: the last held-out set (held-out 4, 20 questions by the fixer, hashed before the review fixes' cue rules were written): precision 0.83, recall 0.50 - the recall bar (0.85) is not met; every other set (written, held-out 1-3, the reviewers' 52) is in-sample. Review round 3 (40 new questions, written before running the router: recall 0.55) added cues that make its set in-sample (20/20) and held-out 4 no longer clean (0.86 / 0.60, its new hit a phrasing section 6 had named): no clean held-out set is left. A missed choice question whose words may ask for a choice is at most `met_with_inference`; one without such words can still be judged `met`. |
| D34 | Debug ledger (loop detection, strategies) | partial | Built 2026-09-25 (section 7): `verinoda debug start/try/status/diff/close`, strategies `differential/bisect/rerun/observe`, MCP `debug_start` / `debug_attempt` / `debug_status` / `debug_strategy` / `experiment_run`, schema v6. debugloops_v1 (12 sessions written by the builder, gold fixed before the rules ran; in-sample after three fixes): definitive precision 11/11, loop recall 8/8, 0/4 controls stopped, top strategy 8/8. A review found 27 problems (25 distinct: false stops, false "passed", unverified bisect ends, git-safety gaps); all fixed with regression tests (section 7.5), the benchmark scores unchanged after the fixes. Not built: a real agent session with and without the protocol. `debug try` overhead is copy-bound on big trees (median 2.4-5.0 s on 2,341 files, depending on machine load). |
| D35 | Change review (`verinoda review`) | partial | Built 2026-09-25 (section 8): `verinoda review` (working tree vs HEAD, `--base`, `--staged`, a planned change with `--target` + `--change`), MCP `change_review` (34 tools), rule tables in `review_rules.py`, the review stored in `analyses`, a review step in both skills. review fixtures (36 dev + 11 held-out, gold hashed before any rule; one documented gold amendment before the first run): dev, in-sample, precision 0.92 and recall 1.00 at strong_inference or above; held-out, its only run with the rules frozen: precision 0.79 (bar 0.8 not met), recall 18/18, must-say-unknown 2/2; 0.81 after seven later fixes (one from that run, six from reviewing Verinoda's own branch; no longer clean). Time with the graph loaded: 0.19-0.21 s median on the examples, 1.8 s median (5.4 s max) on the 380-file copy. First review round (section 8.5): the 41 findings of two reviewers fixed with regression tests (the time finding partly): `--staged` reads the staged tree everywhere and runs it or refuses, SQL must be SQL-shaped, guard refactors are told apart from removals, removed methods and module-attribute call sites are found, a value changed on one line and saved later is found, `no_test_reaches` only for symbols with a static caller (`reach_unknown` otherwise); after it dev 67/73 = 0.92 and 62/62, held-out 22/27 = 0.81 and 18/18 (no longer clean), 1.7 s median on the copy. Second review round (section 8.6): 20 findings of a second reviewer fixed with regression tests - a check that now runs after the work it protected is `guard-after-work` / `check-call-after-work`, security calls are compared call by call, assigned aliases and renamed re-exports reach the guards engine, callers newer than the snapshot are searched and named (`graph_stale`), edges between a monorepo's packages are kept; dev and held-out numbers unchanged, +9 to +13 % time on the copy. Not built: findings as claims and critique on them, the entail predicate for a carried value, line-level coverage of changed lines, nested-loop and unbounded-append rules, value and parameter flow outside Python. |
| D36 | Behaviour probe of changed functions | partial | Built 2026-09-25 (section 9): `verinoda probe FILE::NAME` / `--changed`, MCP `change_probe` (the design named it `behaviour_probe`): inputs from the syntax tree only (annotations, call-site literals and recipes, boundaries mined from comparisons, `len` checks, slices and imported constants of both versions and their callees, standard edges, then hypothesis or a fixed pseudo-random list), one corpus run at the base commit copy and in the working tree through `experiments.run` with a pytest plugin (no new allowlist entry), difference classes with minimal examples reproduced in a second pair of runs and recorded as run-scoped `experiment_verified` claims, properties, undeclared exceptions, nondeterminism, `--scaling`; a static side-effect gate (closure + module-level statements) and an audit hook in the run. No schema change (the design's `probes` table: runs are experiments, results are files under `runs/<probe id>/`). Hand fixtures (49, gold first, in-sample): 22/22 detected (20/20 of those the tests miss), 0 differences on 11 behaviour-preserving edits x 5 seeds (a 12th, labelled equivalent, really changes floats on Python 3.12: reported as `numeric_drift_only`), gate 9/9 refusals and 0/4 wrong ones, median 2.0 s per probe; automated mutants 25/25 killed; unchanged after a review round whose 16 findings were fixed or documented (threads and `multiprocessing` children blocked at run time, a taken module name, process exits, plugin errors, float drift, SQL strings in the gate, `--changed` no pass when a function was not compared), and after a second round of 8 (finalizers and exit handlers blocked at run time, the gate's SQL rule following helpers, defaults, attributes and loops again, float drift only between float literals, no pass when fewer than half of the inputs returned or raised, `asyncio.run` not network, an unparseable changed file, class-state writes, emitted tests for sets and long integers). Not built: `review --probe` (D35), the second minimisation round, the static concurrency signal; methods need a literal-argument constructor call. |
| D37 | Exact names, one build at a time, freshness on every read (section 10) | implemented | Built 2026-09-26: `naming.resolve` for trace, impact and `node_inspect` (copies give way, ties listed), the copy rule in plan linking, the receiver pass bound to what a file can see, `buildlock` around scan/update, `freshness.check` on query/trace/map and the MCP read tools, analyze's refresh outside its budget (skipped with the stale files named when slow; MCP refreshes in the background). Tests from the evaluators' repros (`tests/test_exact_and_fresh.py`); the eight benchmark sets unchanged. Not done: per-file incremental update (the refresh is still a full rebuild). |
| D38 | JVM callbacks and mod entry points | partial | Built 2026-09-26 (section 11): a Java / Kotlin method reference passed as an argument is a `registers` edge (never `calls`), stored in the receiver-call sidecar (version 3); `trace` follows it only when no call path exists, labelled `callback`; impact (map, UI, review dependents) follows it after the other edges, marked `registers (callback)`; a relation claim "A calls B" supported only by a method reference (source line, graph edge or a resolver answer) is graded `registers` (none); the map's entry points know fabric.mod.json, Fabric initializers, `@Mod`, `@EventBusSubscriber` / `@SubscribeEvent`, mixin handlers and registered callbacks, and its sinks JVM file writes, `NbtIo` and dirty flags. fastbench: 0 fact changes on all nine sets. Not built: `analyze` flow claims through callbacks, lambdas passed as callbacks, static initializer blocks, inherited targets. |
| D39 | Honest verdicts | implemented | Built 2026-09-26 (section 12): `verinoda/verdict_gate.py` runs in `analyze` after the claims are made and only caps or refuses (definitions answer only locate questions; copies and reference trees never make `met`; unresolved call sites are named and cap callers; a commit line is not a reason; set differences are `not_supported`). Verdict audit (`verinoda benchmark verdict-audit`, 17 traps + 22 controls on public material, split before tuning, written by the rule author): wrong met dev 9/23 -> 0/23, held-out 8/16 -> 1/16; controls kept dev 13 -> 12, held-out 7 -> 7. No fastbench fact lost. Not done: reasons in comments, a computed set difference, synonyms. |
| D40 | Query ranking: tests yield to the code they test, named files and modules, docstring phrases, narrower expansions | implemented | Built 2026-09-26 (section 13) in `search_index.rank` / `analyze_query`, no index change. Dev set (37 questions, written for the change: in-sample): gold file first 13 -> 28, MRR 0.535 -> 0.836, tests in the top 5 of questions not about tests 61/170 -> 26/170. Fastbench: no fact lost, 2 gained. Not done: common English words that are module names get the weak plain-word boost; a lowercase owner still accepts methods (`asyncio.run`). |
| D41 | Documents and images next to the code: text views of PDF/Office files, Windows OCR | implemented | Built 2026-09-26 (section 14): `doctext.py`; pages, sheets, slides and headings become graph nodes (the markdown extractor over the view), search passages, lexicon words, evidence lines and anchors. Fastbench on fresh indexes: 0 differences over 9 sets. Not done: scanned PDFs (no text layer), audio/video, OCR outside Windows. |
| D42 | A faster `update` with the same graph | partial | 2026-09-26 (section 15): Leiden in native code by default, compact graph.json writes, memoised path and stem work. Same graph, labels, lexicon and receiver edges as before on four corpora (a 4,690-file mod included). Not done: an update proportional to the change; the cross-file passes still run over the whole corpus. |
| D43 | Name check for Java: classpath, JDK and Mixin targets | implemented | Built 2026-09-26 (section 16): `jvmclass.py`, `codecheck_java.py`. On a real Fabric mod (407 files, 126,848 sites, compiled): 0 absent, 2.3% unknown, 4.7 s; 8 of 8 planted invented names caught (Yarn names, arity, a Mixin target). Not done: Kotlin, Groovy, argument types, Maven's classpath. |
| D44 | `update --fast`: the changed files now, the graph in the background | implemented | Built 2026-09-26 (section 17): `workflow._deferred`, `workflow.start_background_update`; MCP `index_update` takes it after a graph build over 15 s. 3.4 s instead of 27-33 s on Verinoda's own repository; the background graph equals a forced scan's. Not done: a graph build that reads only the changed files. |
| D45 | Name check for Kotlin, in the Java check's world | implemented | Built 2026-09-26 (section 18): `codecheck_kotlin.py`; the universe reads Kotlin sources (Java sees them too), `jvmclass` reads `@kotlin.Metadata` names. kotlinpoet: 10 absents, all kotlin-reflect names the given classpath lacked; the Java mod unchanged (0 absent, 8 of 8 caught). Not done: argument counts, receiver-less calls, type inference beyond declarations and constructors. |
| D46 | Import check for TypeScript and JavaScript | implemented | Built 2026-09-26 (section 19): `codecheck_ts.py`. A planted sample: 6 of 6; ky: 0 false absents over 594 sites. A file is checked for imports only and stays under `not_checked`. Not done: calls, members, types (the TypeScript compiler), bundler aliases, Vue/Svelte files. |
| D47 | When a method runs (`verinoda when`, MCP `run_when`) | implemented | Built 2026-09-26 (section 20): JVM lambdas handed to a registration or scheduler as `registers` edges (event, delay), `when.py` walks back to the event with the conditions around each call; "when does X run / ne zaman çalışır" in `analyze`. fastbench on fresh indexes: 0 differences. Not done: Kotlin lambdas, anonymous listener classes, annotation-registered events. |
| D48 | Mixin edges | implemented | Built 2026-09-26 (section 21): `jvm_mixins.py`; `injects` / `accesses` edges with the target method, point and `cancellable`; shown by query (`mixin:`), node, when and analyze (what blocks X). Two ranking changes tried and reverted (21.4); fastbench 0 differences. Not done: bytecode checks of descriptors, `@Shadow`. |
| D49 | GameTest registry and the tests a change should run | implemented | Built 2026-09-26 (section 22): `gametests.py` reads the `fabric-gametest` entrypoints; impact and change_review list the registered GameTest classes that reach the change, nearest first (directly or through a class that calls it), and warn about unregistered ones. Not done: NeoForge, client game tests without `@GameTest`. |
| D50 | Backlog items and code comments | implemented | Built 2026-09-26 (section 23): `backlog.py`; `verinoda backlog <item | file:line | symbol>`, `backlog:` lines in query, `backlog` in node_inspect; a line is explained by the comments on and above it and the declaration comments of the fields it uses. Not done: analyze claims from an item, other backlog formats. |
| D51 | Java access, constructor types, `api` for Java | implemented | Built 2026-09-26 (section 24): class files keep access bits and parameter types; protected/private/package members out of reach and constructors no argument list fits are absent; `verinoda api` lists a Java class's real members from the classpath. A private mod: 0 absent over 127,176 sites. Not done: method argument types, SCIP. |
| D52 | Datapacks: function calls, tags, scoreboard | implemented | Built 2026-09-26 (section 25): `.mcfunction` files in the graph (`calls`, `schedule` as `registers` with ticks, `#minecraft:tick` / `load` as events); `datapack.py` links entity tags and objectives across mcfunction and Java; `verinoda datapack` lists tags checked but never added, objectives written but never read, missing functions. fastbench: 0 differences after keeping mcfunction files out of search names. |
| D53 | Stack traces and GameTest results of a log | implemented | Built 2026-09-26 (section 26): `trace_log.py`; `verinoda trace-log FILE` maps project frames, folds the rest, ties a trace through a test's succeed/fail to that test, stores claims with the log as agent-report evidence. |
| D54 | Shaders: uniform blocks and their Java writers | implemented | Built 2026-09-26 (section 27): `shaders.py`; `verinoda shader FIELD` / `--check`; analyze answers "where does `Block.Field.x` come from". Not done: shader functions in the graph, blocks filled in loops. |
| D55 | English questions over Turkish-named code | implemented | Built 2026-09-26 (section 28): a symbol's leading comment is its own search text (schema 5); the seed dictionary read backwards with Turkish endings; 16 generic seed words. 30 mixed questions: top-3 8 -> 18 (English 0 -> 8). fastbench: analyze and text unchanged, JSON retrieval -4 facts. Not done: comment-learned pairs, a Turkish stemmer in the tokenizer. |
| D56 | An analysis said once | implemented | Built 2026-09-26 (section 29): changed files listed once, uncertainties without repeats, six context claims, critique clipped; analyze facts unchanged, -1.6 % characters. Smaller passage budgets measured and dropped (facts lost). |
| D57 | Java overloads; answers read as a person would | implemented | Built 2026-09-27 (section 30): each Java overload is its own node and a call binds to the overload its argument count fits; `when`, `trace` and name lookups take the whole overload group; "how does X work" with one subject is answered by what X calls (inference), entry-to-storage paths are matched by node, not label; a storage question gets the entry-to-storage paths through its own code; impact names the callers of the method asked about; a symbol's doc comment above its span prints where it is; the JSON answer keeps room for the next two candidates. JSON retrieve facts 253 -> 262, text and analyze unchanged. |
| D58 | Less noise in an answer | implemented | Built 2026-09-27 (section 31): context the critique refuted is counted, not printed; a changed file is an unknown only when it spells a name that looks like one (not a plain word that happens to name a function); "how does X decide ..." is a mechanism, not a setting; `module.function` links to that module's function; no empty quote claims; the history view 100x faster; a why-answer quotes the section with the reason. |
| D59 | Settings read by a string key | implemented | Built 2026-09-27 (section 32): a config question also gets the string-keyed setting reads (`Config.getInt("car.door-ticks", 140)`) in the files that ranked for it, matched by the question's words (and their Turkish glosses), with the default and the YAML / TOML / .properties line that sets the key. |
| D60 | What an agent carries and cites | implemented | Built 2026-09-27 (section 33): passages number their lines (blank lines left out); the core MCP menu and instructions are about a quarter shorter and list decision_check only in a project with decision records; `index_update` scans a folder never scanned; the skills and instructions say to cite the narrowest lines and to run code_check on code written, not read. |
| D61 | A four-tool menu and a gateway | implemented | Built 2026-09-27 (section 34): the core profile lists project_query, analyze, code_check and index_update (analyze and code_check with the arguments a question or an edit needs) and `run_tool`, which reaches the other core tools by name with their own argument checks; an agent session's first turn is 2,177 tokens larger than without Verinoda, 3,933 before. |
| D62 | Verinoda in the Grep the agent already runs | measured, off | Built 2026-09-27 (section 35): `grep_context` answers a Claude Code PostToolUse hook on Grep with the definition, callers and callees of a searched symbol (at most 450 characters, nothing when the name is unknown); the hook ships as a template, opt-in; `ANALYZE_FIRST` holds the sentence that asks for analyze before a search by hand. An adoption study (125 sessions) switched neither on: neither found more facts; both stay built and off. |
| D63 | Running a project's own tests safely | implemented | Built 2026-09-28 (section 39): `verinoda trust` records trusted projects outside every repository; an untrusted project's tests run only in a container, else they are refused with a next_step; `experiments.*`, `mcp.profile` and `research.network` come from a project's own config only when it is trusted; argv[0] must be a known interpreter or a bare name found on PATH without the current directory; pytest `@file`, `-p NAME`, `-o addopts=...` and the config files' addopts and path settings are checked; the copy follows no link; the container runs read-only, without capabilities, as the host user. OS confinement without Docker is not done. |
| D64 | Ranked unknowns, declared types | implemented | Built 2026-09-28 (section 36): unknown Python sites are ranked HIGH (a name defined nowhere in a word index of the project, its environment and the stubs, or a close misspelling of the receiver's declared type), MEDIUM or LOW; LOW sites are counted by cause in `unknown_summary` and listed only with `--all`; sites are listed absent, HIGH, not installed, MEDIUM, guarded, LOW, and MCP `code_check` gives one line per site; declared types (jedi's inference, comprehension `for` targets, pathlib joins, declared return types, pytest's own fixtures) decide `exists`, never `absent`; `with raises(E)` guards an import and a module listed by its exact name in any requirements file is `not_installed` (optional); the environment's word index is kept in the user cache and resumed where its budget stopped it. On a planted-misspelling diff: 50 planted sites all absent/HIGH/MEDIUM, 0 real HIGH, real unknowns 125 -> 70, planted sites in the first MCP answer 12 -> 42. Not done: `**kwargs` following, mypy/pyright, runtime probes. |
| D65 | A graph with fewer false calls, and the tests of more ecosystems | implemented | Built 2026-09-28 (section 37): member calls bind in the file only through the method's own receiver or a receiver whose type the file states (Python `super()` to an in-file base in C3 order; Go, Rust, PHP, Ruby, JS/TS); no call leaves vendored, minified or generated code (`index.vendored` keeps them); .NET, Xcode, GoogleTest, Dart and Elixir tests are test files; the C# type-reference pass is linear; `.hh .hxx .ipp .inl .tpp` are C++. |
| D66 | Faster scans | implemented | Built 2026-09-28 (section 38): detect() takes the file list from git at the top of a work tree (git's own .gitignore semantics; the same corpus as the walk on the seven trees measured, known differences listed; the walk still runs for nested and embedded repositories, submodules, negated .graphifyignore rules, non-UTF-8 ignore files, unreadable directories; a rebuild never evicts a file git keeps), the receiver sidecar's Python parses serve the search index's spans, anchors are written in one transaction from one read, and a from-scratch search index is built in a separate file renamed into place. |
| D67 | Issue-shaped questions | implemented | Built 2026-09-28 (section 40): a typed question is never refused because the plan drafted for it failed its own checks (the draft carries every version the message names within the reference limit; a drafted plan that still fails is kept for the record and the question is answered as one sub-question about its mentions, saying so; a host's plan is still refused); the answer's language is the prose's (code, URLs and English contraction tails do not count; English function words outnumbering Turkish signals three to one make it English unless the user's own first or last sentence is Turkish); an issue-sized message's drafted "understood as" is at most 300 characters and a sub-question's text is printed once, clipped to 160 characters, so the passages come earlier. |
| D68 | Verinoda's own files are not the project's | implemented | Built 2026-09-28 (section 41): files carrying the ownership marker in a skill folder of the installer's shape (at any depth, any letter case) or listed by the install manifest are left out of the snapshot's file list (and both sides of the debug ledger's tree diffs), the graph (as build excludes that also evict an older graph's nodes, plus a post-build drop) and the freshness check; `.mcp.json` stays in the corpus but Verinoda's own server entry is not extracted and a change confined to it does not rebuild the graph (a digest of the config without that entry is recorded with each build); setup writes the agent files before indexing. On a 2,623-file project a repeated setup no longer rebuilds the graph: 40 s -> 1.3 s for the run after a first setup, 22-23 s -> 5.1-5.5 s for setups alternating two installs (the rest is the derived-data refresh of `.mcp.json`, not changed). |
| D69 | Datapack functions called from Java | implemented | Built 2026-09-28 (section NN, GitHub issue #1): `datapack_java.py`; `verinoda datapack function` lists Java callers as their own records (`via`: command string that reads as a command, the function manager's lookup of an identifier, or a helper recognised by its body and keyed by class, name and argument count; `datapack.function_helpers` for others; a call binds only where Java resolves it: imports, packages, nested classes, the caller's tree), labelled `[test]` and `[reference tree NAME]`; constant tables and `String.format` are read; names built at run time are listed as dynamic (`ns:prefix_*`); a Java call to a missing function is a mismatch. A private mod: 0 -> 68 Java calls, the issue's four functions 0/7 -> 7/7 Java callers, 0 false positives, +0.3 s. Not done: graph edges, Kotlin. |

Delivery plan (section 5): step 1 (round 3) and step 2 (integration) are done.
Step 3 (measurement) is in progress: see BENCHMARKS.md for which numbers
describe the current code. Step 4 (adversarial acceptance audit) has no
recorded result for the round-3 code in this repository.

## 0. Goals, in priority order

1. **Understand what the user is asking.** The user may write in Turkish or
   English. The question may be vague, may contain several questions, or may use
   domain words that do not appear as identifiers in the code.
2. **Resolve every reference the user gives** (repository, file, symbol, commit,
   PR or issue, package, paper, documentation page, application):
   - pin it to the exact version the user meant;
   - never swap in the current main branch for an older version;
   - say explicitly what could not be resolved.
3. **Be more efficient than Graphify, measured on the benchmark:** more gold
   facts per token delivered to the model, fewer wrong or unsupported
   statements, and per-query latency in the same range as Graphify.
4. **Keep the evidence discipline:**
   - claims come with evidence;
   - an answer is `unknown` plus a next step rather than a guess;
   - claims go through self-critique;
   - user critique is treated as a hypothesis to test;
   - history is append-only.

The core stays deterministic: no LLM runs inside Verinoda at index or query
time, and the network is used only for explicit research. The host agent
(Claude Code or Codex) supplies the language understanding. Verinoda checks
that understanding against the code.

## 1. Question understanding

### 1.1 Findings that drive the design

- Measured on `examples/orders_app` [R-intent]:
  - All 4 Turkish paraphrases of benchmark questions retrieved **0** items; the
    English originals put the gold symbols in the top 4.
  - A prototype recovered the gold symbol at rank 1–2 in 4 of 4 cases. It folds
    Turkish characters, strips suffixes, and expands terms through a small
    TR→EN dictionary. An expansion is kept only if its English target exists in
    the repository.
- Normalisation defects [R-intent]:
  - `İndirim` → `ndirim`, because of the combining dot after lower-casing.
  - The dotless `ı` is not folded.
  - Turkish question words are not stopwords.
  - Apostrophe suffixes (`'deki`, `'ı`, `'ın`) become search terms.
  - `db` is dropped because it is shorter than 3 characters.
- Intent rules misread the benchmark's own questions [R-intent]:
  - "what decides the database file" is read as a `why` question.
  - The second clause of a compound question is lost.
  - Turkish stems collide with domain nouns: `kayıt`, `gider`, `yazar`.
- What the analysis understood is not stored anywhere, so an answer cannot be
  audited against its interpretation.
- The literature points to one pattern:
  - The LLM produces a structured query or hints, and a deterministic engine
    validates and executes it (AutoCodeRover, Agentless, LocAgent, LogicLoc,
    Reformulate-Retrieve-Localize).
  - Names the LLM proposes must be grounded in the corpus (HyDE).
  - Ambiguity should be detected from grounded data and resolved with a few
    multiple-choice questions (AmbigQA, CLAM, ClarifyGPT, Ambig-SWE).
  - For Turkish retrieval, 5-character prefix stemming works about as well as a
    lemmatiser (Can et al. 2008).

### 1.2 Decisions

**D1. Question plan contract `verinoda.question_plan/1`.**

The host agent fills a JSON plan from the user's message. Verinoda validates
it (`verinoda/question_plan.py`; the schema ships as package data).

A plan contains:

- the user's message, verbatim;
- the restated goal, in English and in the user's language;
- sub-questions, each with an intent and a checkable `done_when`;
- mentions, in the user's own words, with an English gloss and candidate names;
- references, with the version exactly as the user meant it;
- constraints and assumptions.

Transport:

- MCP: the `plan_json` string.
- CLI: a file under `.verinoda/plans/`. JSON is never passed on the command
  line, because Windows PowerShell 5.1 mangles it.

**D2. Deterministic validation before any work.**

Validation checks:

- the schema;
- that ids are unique and resolvable;
- that sub-question dependencies form a DAG;
- that every mention or reference text occurs verbatim in the message;
- that every version-like token in the message is carried by some reference
  (`version_dropped` otherwise);
- that relative-version words ("old", "önceki") produce a clarification;
- limits (at most 6 sub-questions, 20 mentions, 10 references);
- a divergence check against the rule-based intents, which reports differences
  without overriding the plan.

**D3. Every mention is grounded in the graph with evidence.**

Matching runs in evidence tiers: exact id, path, `Class.method`, label,
folded label, identifier parts, fuzzy match (rapidfuzz), repo-learned lexicon,
seed dictionary, text hit. Candidates proposed by the host that match nothing
are rejected and never used as search seeds.

Results:

- **linked**: score ≥ 0.70 with a margin of 0.15 over the next candidate.
- **ambiguous**: at most 3 grounded multiple-choice clarifications, from fixed
  TR/EN templates. Before asking, a graph probe checks whether the choice would
  change the answer. If it would not, the candidates are merged silently.
- **weak**: used, and the resulting claims carry an uncertainty.
- **unlinked**: reported as `unknown` with a next step.
- **not_found** (D31): a mention written as code with no exact name, spelled
  nowhere in the repository (a member of a known class or module: nowhere in
  that owner); never replaced by a similar name.

**D4. The same code path with or without a host plan.**

Without a host plan, `question_plan.draft()` builds a plan from rules:

- bilingual TR/EN cue tables;
- Turkish case suffixes mapped to roles (ablative = source, dative = target, …);
- clause segmentation that splits only when a clause has its own question cue;
- anaphora (`bunu`/`it`) resolved through `subject_from`.

Every element the rules produce is tagged `derived_by`.

**D5. Text normalisation** (`verinoda/textnorm.py`):

- length-preserving Turkish folding;
- apostrophe splitting;
- Turkish stopwords;
- a short-technical-term keep-list (`db`, `id`, `ui`, …);
- a `tr_stem` that accepts a stem only if it prefix-matches the repository
  vocabulary, with a 5-character prefix as fallback.

This normalisation is applied to both the query terms and the scanned text.

**D6. Repo-learned lexicon** (`verinoda/lexicon.py`, built at scan/update time
into `.verinoda/index/lexicon.json`):

- Associates natural-language words with identifier parts. The words come from
  docstrings, comments, string literals and README/doc sentences that contain a
  backticked identifier.
- Association uses Dunning G² ≥ 10.83 (p < 0.001) and a support of at least 2.
- Each pair keeps up to 3 evidence sites (`file:line`).
- A small seed dictionary covers generic software terms (veritabanı → database)
  and common business domains (sipariş → order, fatura → invoice).
- A seed entry is used only when its English target exists in the repository.
- Lexicon pairs only ever produce candidates; they are never evidence for a claim.

**D7. Execution per sub-question.**

- Handlers are keyed by intent and run in topological order.
- The budget is split across sub-questions.
- Each sub-question gets a verdict against its `done_when`: `met`,
  `met_with_inference`, `unmet`, `not_supported` or `blocked_by_clarification`.
  Since 2026-09-25 a context claim (the definition of an item the search ranked
  near the question) counts for `met` only when it is about the sub-question's
  subject: a symbol the question names or links, a member or owner of one, or an
  item carrying every group of the question's words (`flags.off_subject` lists the
  others). Before, "where is an order written to the database?" could be met on
  a verified definition of the settings loader.
- An analysis carries the passages `verinoda query` gives for the same question
  (`passages`, a list of lines), so it never has less to go on than a search.
- `verinoda plan audit <analysis>` recomputes the verdicts later and marks a
  sub-question `stale` when a claim it relies on went stale.

**D8. Plans are stored.**

- Schema v3 adds a `question_plans` table.
- A plan body is immutable; changes are revisions linked through `parent_id`.
- Analyses reference their plan.
- A "you misunderstood me" critique becomes a plan revision; the old plan and
  its analysis are kept.

**D9. Skills get an "understand the question first" protocol.**

1. Draft the plan.
2. Edit it: split compound questions, gloss domain words, copy versions as the
   user meant them.
3. Check it.
4. Ask the clarifications: `AskUserQuestion` in Claude Code, `request_user_input`
   or plain text in Codex.
5. Run the analysis.
6. Answer, starting with "Understood as / Anladığım: …" and continuing with one
   block per sub-question.

### 1.3 Measurements this must move (benchmark schema 2)

- Intent macro-F1.
- Segmentation exact-match.
- Mention linking accuracy@1 and recall@5.
- Rejected-hallucination rate.
- Clarification precision and recall.
- Unlinked mentions reported as unknown: target 100%.
- The fact-recall gap between Turkish and English variants of the same
  questions: target close to 0.

## 2. Reference resolution

### 2.1 Findings that drive the design

The current `parse_reference` was run on 20 reference forms. It broke the "exact
version the user meant" rule in 11 of them [R-refs]:

- Four URL forms were pinned to default-branch HEAD even though the URL names a
  version: compare, releases/latest, archive tags, GitLab merge requests.
- PR, issue and raw URLs were scraped as undated secondary pages.
- `#L100-L120` line anchors were dropped.
- When a tag and a branch share a name, git picks the tag, while GitHub's web
  interface shows the branch.
- Unversioned arXiv ids fetch the latest version (v7).
- `docs.python.org/3/` is the latest Python, not the project's version.
- `owner/repo`, `pkg==x.y`, SWHID and bare DOI inputs were rejected.
- "The version we use" cannot be answered, because lock files and installed
  metadata are not read.

Mechanisms verified live [R-refs]:

- **Content match.** File blob hashes were compared between a package's
  published artifact and the repository at each candidate ref, using a blobless
  bare clone (`git clone --bare --filter=blob:none`, 3.8 MB, 5 s). The requests
  2.31.0 sdist matched `v2.31.0` 40/40, `v2.30.0` 34/40 and `main` 8/40.
- **PEP 740 provenance** gives the source commit using only the standard
  library.
- `git ls-remote` exposes PR heads without the REST API.
- The Go proxy `.info` file carries `Origin{URL,Ref,Hash}`.
- Crates ship `.cargo_vcs_info.json`, which records the source commit.
- The Read the Docs API maps a docs version to a commit.

### 2.2 Decisions

**D10. `verinoda/references/`: parse → classify → bind → resolve → pin → fetch
→ extract → report.**

- Mentions are extracted from TR/EN text.
- Classification is table-driven and covers GitHub/GitLab URL shapes,
  `owner/repo[#N]`, `@sha`, purl, `pkg==v`, `npm@range`, `module@v`, Maven
  coordinates, arXiv, DOI, SWHID, paths, symbols and application cues.
- Versions are bound to the nearest reference in the same clause.
- The output schema `verinoda.reference_resolution/1` accounts for every
  mention. Each one is either bound to a pinned reference or reported as
  unbound or unresolved, with a reason and a next step.

**D11. Pin precedence ladder** (recorded as `pin.basis`):

1. An immutable id in the reference itself.
2. A version stated in the text.
3. An explicit request for the floating version ("main", "latest", "en son").
4. A tag in the URL.
5. The version the local project uses (lock file, then installed metadata,
   then runtime pins).
6. A branch in the URL. This drops below rule 5 when the question is about
   local behaviour.
7. A date mentioned in the text.
8. The floating default, which is reported with a warning.

A named version never falls back to a floating ref.

**D12. Mismatch catalogue M1–M12, always reported and never resolved silently:**

- M1: text version vs URL ref.
- M1b: path missing at the pinned version.
- M2: URL ref vs local version.
- M3: floating docs vs the project's runtime version.
- M4: paper version.
- M5: tag/branch name collision.
- M6: PR force-pushed or squashed.
- M7: repository moved.
- M8: published artifact does not match its claimed source.
- M9: yanked or deprecated version.
- M10: version not found.
- M11: docs version not hosted.
- M12: source not public.

**D13. Local version resolver** ("the version we use"):

- Lock files: uv.lock, pylock, poetry.lock, requirements `==`,
  package-lock.json, Cargo.lock, go.sum.
- Installed metadata from the project's own virtual environment, read with
  `importlib.metadata`. Project code is never imported.
- Runtime pins: requires-python, `.python-version`, `.nvmrc`.
- Each result is source evidence (`file:line` plus hash), so it goes stale like
  any other claim.

**D14. Package → source, ranked by strength:** content match, then signed
attestation, then publisher-declared VCS info, then tag name, then date.
Registry metadata only points to a candidate and is never counted as
verification.

**D15. Offline-first transport.**

- Modes: live, cache, cassette.
- CI runs without network: sockets are blocked, and a missing cassette entry
  fails the test.
- HTTP requests are rate-aware and send no credentials in logs or cassettes.

**D16. Integration.**

- `feedback.process` resolves the references first. If any reference the
  critique relies on is unresolved or conflicting, the verdict is `unresolved`
  and the answer names the conflict.
- A dependency that matches the locally locked version becomes
  `dependency_source` evidence (rank 4) instead of `reference_repo` (rank 6).
- New entry points: CLI `verinoda resolve "<text>"` and the MCP tool
  `reference_resolve`.

### 2.3 Measurements

- A golden corpus of at least 60 TR/EN questions containing references.
- Precision and recall per mention kind.
- Share of mentions accounted for: 100%.
- `silent_floating_pins`: 0.
- Mismatch recall of 100% on seeded conflicts, with 0 false positives on 20
  clean cases.
- The whole suite passes with the network disabled.

## 3. Retrieval efficiency

### 3.1 Findings

- **Profile [R-profile]** (graphify_core: 226 files, 4,094 nodes):
  - Median per-question time is 6.9 s for `verinoda query` and 8.0 s for
    analyze.
  - Graphify takes 35–94 ms with the graph already in memory, or 0.6–1.6 s
    through its CLI.
  - Where the time goes:
    - a per-question scan of every file: 74–93% of wall time;
    - the receiver-call pass on every graph load: 1.6–1.8 s;
    - critique re-hashing the whole tree once per claim: 12–22%, and 92% of
      analyze on the small repo;
    - symbol spans computed with one AST walk per symbol.
  - A silent 400-file cap left 459 of 859 files unscanned on the full corpus.
- **Output overhead [R-retrieval]:** envelope, reasons and edges make up 52–61%
  of JSON retrieval output. Excerpts are always the first 14 lines of a symbol,
  not the lines that matched.
- **Prototype [R-retrieval].** It combines a persistent passage-level BM25F
  index, a personalized PageRank prior, and skeleton-first plain-text rendering
  with call outlines. Scored with the benchmark's own gold-fact scorer:

  | Set | Prototype | Graphify | Current Verinoda | Raw |
  |---|---|---|---|---|
  | graphify_core | 36/37 (1,491 tok/q) | 7/37 (1,667) | 18/37 | 4/37 (5,990) |
  | held-out, written before any run | 28/33 (1,487) | 17/33 (1,659) | 11/33 | 7/33 (5,988) |

  - Warm query latency: 20–30 ms.
  - Ablations: without the call outline, 3 fewer facts on each set; without
    PageRank, 1 fewer.
  - Caveats: graphify_core is in-sample (the design was made after seeing its
    misses). The held-out set is small and Python-only.
- **Literature:**
  - Skeletons beat full files. Agentless: 58.3% vs 53.7%, at about 1/7 of the
    cost.
  - BM25 with an identifier-aware tokenizer is strong on repository QA.
    CodeRAG-Bench RepoEval: 93.2 vs 83.8 nDCG@10.
  - Graph expansion from lexical anchors helps (LARGER, LocAgent).
  - Concise, informative tool output helps (SWE-agent ablations).

### 3.2 Decisions

**D17. Persistent passage index** in `.verinoda/index/search.db`. It is kept
apart from `atlas.db` because it is disposable.

- Units: symbols (their own lines only), module-level blocks, prose sections.
- Passages: 12-line windows with a stride of 6.
- Tokenizer: each compound identifier is indexed whole and as its camel/snake
  parts, stemmed.
- Incremental per changed file, driven by the snapshot diff.
- Versioned by tokenizer and schema.

**D18. BM25F ranking.**

- Fields: name (weight 3.0, b 0.3), path (weight 0.5), body (b 0.75); k1 = 1.2.
- idf is computed per unit, so long functions do not inflate it.
- A unit's score is its best passage's score, so a 3,779-line function becomes
  about 600 competing passages instead of one giant bag of words.
- An identifier the question spells out exactly sets that unit's lexical score
  to 1.0.

**D19. Graph prior.**

- Personalized PageRank computed by pure-Python local push
  (Andersen–Chung–Lang): α 0.3, ε 1e-4, at most 20k pushes. `nx.pagerank` is
  not an option because it needs scipy.
- Seeds are the top lexical units.
- Edge weights depend on relation and direction; INFERRED edges get ×0.7.
- Final score: `lex + λ·p/(p+κ)`, with λ 0.4 and κ 0.02.

**D20. Plain text for the model, skeleton first, packed to a budget.**

- Top 3 items get:
  - a `path:a-b` header with the signature;
  - the first doc line;
  - `calls:` and `called by:` outlines with call-site lines;
  - referenced module constants;
  - the best matching passages.
- Items 4–7: the header plus one passage.
- The rest: one skeleton line each.
- Truncation is always stated, with the follow-up command.
- JSON output stays for programs.
- Nothing is printed twice (2026-09-26): no echo of the question; `expanded:` shows at most
  three `from->to` pairs (the JSON keeps all, with why); the header names the item without its
  signature when the passage below starts at the item's first line (the signature is that
  line; the header takes it back if the passage is cut); each passage window is dedented on its
  own; the tail says `next: verinoda query "…" --max-chars N` (the CLI named as such: MCP
  clients read the same text, and project_query takes no budget). The `## path:a-b` and
  `  path:x-y` locators are unchanged (measured in docs/BENCHMARKS.md, "Update 2026-09-26:
  token wins").
- The note on what is left out is never dropped (review fix, 2026-09-26): at a budget too small
  for the whole note it takes a shorter form (the list cut to the room, the count with the next
  step, the count alone), and when even that does not fit next to the top block the block goes
  to the list; with no block left the text says "N candidates not shown (budget too small)",
  never "no candidate locations".
- `analyze` follows the same rule (2026-09-26, `analysis_view`): its default text and the MCP
  response carry the answer, not the run - verdicts, answer claims, the other claims, unknowns
  and the query passages; confidence only below its status's cap, evidence only where it adds a
  locator, a verified context claim whose lines a printed passage shows left out and counted.
  Over the MCP cap the critique log and the plan's links are cut before the passages, and the
  passages (from the end) before any claim. `--json` keeps the full record. Review fixes: the
  text prints the plan's links (not the weak ones) as MCP does - a linked word's locator can be
  the only place a fact is found (heldout h03.rebuild); claims are left out as printed only on
  the passage lines the cap keeps (`lean_capped` recounts after the cut, and a window counts only
  the lines that follow it), so a claim whose lines the cut removed is listed again; evidence is
  left out only for the same whole locator (line 18 is not line 180).
- A profile's texts name only what it serves (review fix): hints and parameter descriptions in
  the core profile name the CLI command, and the MCP tool with the profile that serves it. Where
  MCP is the only way in - Codex on an editable or hardlinked install, whose sandbox may not
  import verinoda - install registers `--profile full`, so every protocol the skill makes
  mandatory stays reachable; the skills tell the agent to ask for the full profile when a
  mandated tool is not listed. A config whose `mcp` setting cannot be read is an error, never a
  silent core.
- The skills read the CLI's text, not its JSON (2026-09-26): JSON cost 2-5x the tokens for the
  same content (analyze 3,824 vs 717 tokens, decide brief 13,206 vs 2,985, doctor 2,711 vs 795),
  and the text carries every field the protocol reads. `--json` stays for programs and prints
  compact JSON off a terminal (indentation was a quarter of the bytes); `doctor --brief` prints
  the graph and snapshot lines and every problem, nothing else.
- The budget still does not follow the question by default. A question-shape budget (4,800
  characters for a single-clause question, 6,000 for compound, flow and test questions) exists
  behind `query.shape_budget` / `VERINODA_SHAPE_BUDGET`: -8.3% tokens per question on the 8 sets
  with no fact found lost, but one gold line no longer shown, so it stays off (the rule: no gold
  fact lost anywhere, found or shown, in-sample or out). A stop signal that knows when the
  question is covered is the open lever (docs/BENCHMARKS.md, "Update 2026-09-26: token wins").

**D21. Build-time work is not repeated at query time.**

- Symbol spans come from one pass per file: Python `ast`, and tree-sitter
  `end_point` for other languages.
- Receiver-call edges are computed at scan/update time and stored under the
  file hash.
- A per-file interval index answers "which symbol encloses line N".
- Freshness is computed once per analyze and passed to critique.
- File hashes are cached by stat (size, mtime_ns), with git's racy-clean rule.
- The MCP server keeps the graph, spans, index and caches between calls.
- The upstream path-identity memo is a vendored patch and is recorded in
  UPSTREAM.md.

**D22. Vocabulary gaps.**

- Abbreviations are expanded through corpus prefixes plus a small fixed map
  (env/environment, cfg/config, db/database, …).
- Turkish stopwords and glossary are shared with D5/D6.
- Every expansion is reported in the output header.

Targets on graphify_core:
- query latency under 50 ms warm and under 1 s cold;
- facts per 1k tokens at least equal to Graphify's on held-out sets.

## 4. Trustworthy claims: precise evidence and invalidation

### 4.1 Findings

- **Replay [R-verify] of 300 real Graphify commits:**
  - Only 2.37% of the definitions in modified files changed their normalised
    AST. File-level staleness therefore marks about 97.6% of the affected
    claims stale for nothing.
  - 44% of the unchanged definitions only moved.
  - Import bindings changed in 4 of 26,541 cases.
- **Relocation:**
  - 23.5% of non-blank lines are not unique within their file.
  - A wrong relocation was reproduced: `sys.exit(1)` cited at line 1164 was
    matched to 1090.
  - A symbol anchor (qualified name + definition fingerprint + AST path)
    relocated both test edits exactly.
- **Resolvers [R-precision]:**
  - jedi confirmed 2,768 of 2,798 Graphify call edges and exposed 16 wrong
    ones: 13 shadowed duplicate definitions and 3 alias collisions.
  - jedi found 335 caller→callee pairs Graphify missed; 143 of them were also
    seen at runtime.
  - scip-python needs Node and crashes on Windows. Its symbols are name-based,
    so it would have confirmed the 13 shadowing errors.
- **Runtime:**
  - A `sys.monitoring` tracer driven by PY_START costs 1.23× CPU. For
    comparison, `setprofile` costs 2.25× and coverage with contexts 1.93×.
  - A CALL-based tracer missed functions called back from C
    (`sorted(key=f)`, `map(f, …)`) for 61 of 739 functions.
  - The PY_START tracer reproduced the 4 of 4 gold "tests that reach
    apply_discount".
  - It rejects the benchmark's one wrong finding: the claim that
    `test_empty_order_rejected` reaches `apply_discount`.

### 4.2 Decisions

**D23. Symbol facts** (`verinoda/anchors.py`), cached by file sha256.

- Per definition: a signature hash; a body hash (docstring removed, positions
  dropped, nested definitions Merkle-hashed); a doc hash.
- Per file: import bindings, module statements, doc sections.
- Python uses its own serializer, not `ast.dump`, whose output changed in
  Python 3.13.
- Other languages use tree-sitter node/leaf sequences.
- Anything unsupported falls back to file level, so the result is never less
  safe than today.

**D24. Claim dependencies at symbol/facet level with early cutoff** (the
Salsa/Bazel verifying-trace idea).

- Each claim kind depends on specific facets:
  - location → signature;
  - relation → caller body + name binding + callee signature;
  - flow → the body of every hop;
  - "no test reaches X" → the hash of the test set.
- An update marks a claim stale only when one of its facets changed.
- Claims whose files changed but whose facets did not are rebound (backdated)
  without a status change.

**D25. Anchored evidence.**

- A source citation stores {symbol, fingerprint, AST path, occurrence}.
- Relocation outcomes:
  - same;
  - moved: exact new lines, still OK;
  - changed: a candidate only, never verification on its own;
  - gone.
- Evidence rows stay immutable; relocations are appended to
  `evidence_locations`.

**D26. Verdict rules.**

- Mechanical entailment grades per claim kind (full / partial / none, after
  AIS and Self-RAG). Example: a relation is `full` when the AST has a Call at
  the cited line whose callee resolves to the target or to an import alias of
  it.
- Evidence groups, as in FEVER: a flow's hops form one group. A verified
  status needs one group that is fully graded, fresh and verifying.
- Only a *definitive*, scope-exhaustive refutation makes a claim
  `contradicted`. A heuristic miss lowers the claim by one step and adds an
  uncertainty.

**D27. Counter-hypothesis probes.** Critique runs cheap, tool-driven checks in
the style of CoVe and CRITIC, with no LLM. Example: in a `pytest.raises` block,
a call placed after an earlier raise cannot reach later code. This check
catches the benchmark's wrong finding in 6 ms.

**D28. Runtime observation** (`verinoda/runtime/`).

- An opt-in pytest plugin on `sys.monitoring`, driven by PY_START.
- It records call sites and test contexts only.
- It runs in the isolated experiment runner, with budgets on events, edges,
  bytes and time.
- `experiment_verified` here means "observed in run R at commit C".
- A runtime observation never supports an "always" claim, and test doubles
  never support production edges.

**D29. Optional precise resolution** (`verinoda[precise]` = jedi).

- Lazy: it runs only on the call sites that end up in claims, and results are
  cached.
- A unique function/class answer equal to the target gives
  `statically_verified`.
- A unique answer that differs from the target is a definitive refutation.
- Parameters, dynamic calls and ambiguous answers stay `strong_inference`.
- A real `index.scip` produced by the user is read by a dependency-free decoder
  and gives cross-language references.

**D30. Measurement harness** (`benchmark/staleness.py`,
`benchmark/critique_eval.py`).

- History replay plus a mutation suite, checked against a from-scratch oracle.
- Staleness recall must be 1.0, and zero claims may be silently wrong while
  shown as verified.
- Critique precision and recall are measured on a labelled set.

**D31. No laundering: word overlap never verifies, every role is bound, a
definitive miss is contradicted at once, a code name is never substituted.**
Four ways a false sentence reached a verified or "likely" status (found on a
copy of `examples/orders_app`, 2026-09-25) are closed. All rules are
deterministic.

- **Term coverage is `partial` at most** (`entail._coverage_grade`, code
  `coverage`). "apply_discount returns the subtotal above the threshold" had
  every word of the lines that return `subtotal * 0.9` there, and was
  `statically_verified`. Order, direction, conditions and roles are invisible
  to word overlap. Only a verbatim quote (`<path>:<line> contains: <text>`; a
  quote under 8 characters only as the whole cited line) or a kind's typed
  check is `full`. Text a user or an agent wrote (`spec.free_text`,
  `claim add`) that no typed check covers needs a `full` grade even for
  `strong_inference`, so word overlap gives it `weak_inference` at most
  (`claims._allows`, `entail.typed`). The same holds for a passing run
  attached to a plain-text claim ("the pricing tests pass"): its command line
  sharing the claim's words is relevance, not verification; a `test_run`
  claim that names the run (`spec.experiment` or `spec.command`) is verified
  by it.
- **Every role is bound** (`entail.assess`, free text only; generated claims
  state the roles by construction):
  - a relation's text states one caller and the callee in one clear form
    (`entail.relation_parse`): "A calls B", "B is called by/from/in A", "A, B'yi
    çağırır" (the accusative marks the callee, in any word order:
    "B'yi A çağırır", "B fonksiyonunu A çağırır") and "B, A tarafından
    çağrılır". In a sentence with several clauses the clause that names the
    target is read ("A calls B, and C calls D"); several names on the calling
    side ("both A and B call C"), a cleft ("B is what A calls") or a relative
    clause ("B'yi çağıran A") is no clear form. Then no role is guessed: the
    grade is `partial` ("the text does not state one caller and the callee in
    a form that is checked"), and nothing is contradicted. The callee must be
    the claim's target, and the call-site grade checks the cited line against
    the caller the text names (`Owner.name` also needs the class `Owner`
    around the line). A caller written as a plain word ("checkout calls
    submit") or a file must be the definition around the cited lines or their
    file, else the grade is `partial` and it never drives a contradiction. A
    call verb outranks "using"/"creates", and an infinitive ("to create") is
    not the relation. A call on the receiver the text writes (`repo.save`,
    `self._check`) and `self.m()` inside a class that defines `m` are `full`;
  - a config text's subject ("the discount threshold") must be spelled by the
    name the cited read of the variable is bound to (`entail.env_bindings`:
    assignment target, dict key, keyword, enclosing definitions), at least
    half of its words, else the grade is `partial`;
  - what the text states beyond the kind's typed check keeps it `partial`
    (`entail.unchecked_statements`): a negation ("not", "never", "n't", TR
    "çağırmaz", "okunmaz", "değil": the check proves the positive statement),
    a quantifier ("only", "all", "always"), an order in a non-order claim
    ("before", "after", "then", "first"), in an order claim that the calls
    are adjacent ("immediately before", "right after", TR "hemen"), a
    condition ("if", "when", "provided that", "as long as"; in Turkish text
    the conditional forms "altındaysa", "gelirse", "varsa"), a bound
    ("below", "above", "at least"), a count ("twice"), how a relation's call
    is made ("with two arguments", "with the customer name", TR "müşteri
    adıyla"), and a number, in digits or words ("ten seconds"): a config
    claim's number must be in the cited read itself ("defaults to 50" against
    `os.environ.get(..., "100.0")` is `partial`), other kinds do not compare
    numbers;
  - a file the text names is a role: when it is neither the evidence file nor
    another cited file (nor the file of `--symbol path::name`), the grade is
    `partial` ("`save` is defined in orders/service.py" citing
    orders/repository.py: "the text names orders/service.py, the evidence is
    in orders/repository.py"; also "orders/pricing.py reads
    ORDERS_MAX_ITEMS" citing orders/config.py);
  - a location text's kind of definition ("a function", "a class", "an async
    function", "a method", "a module constant", "a class attribute", TR "bir
    fonksiyondur") is compared with the definition at the cited line
    (`entail.kind_problems`: Python by its syntax tree, other languages class
    or def from their syntax facts); a kind it does not have, or one that
    cannot be read there, is `partial`. Words that name an owner ("of the
    `OrderRepository` class") are not a kind, and a plain word naming a class
    ("a method of the Settings class") is a name the check must bind;
  - so does a code name the check does not bind (`entail.unchecked_names`,
    relation, location and order claims): "create_order_handler calls
    place_order and fetch_order" is `partial` ("the text also names
    `fetch_order`, which the relation check does not establish"). A
    relation's other callee counts when the text lists it with the checked
    callee, joined by coordinators only ("A calls B and C", "A calls B, C and
    D", TR "B ve C'yi çağırır"), or it is another clause ("A calls B, and C
    calls D"), and the caller's whole body calls it directly
    (`entail.caller_scope`). A name after "instead of", "rather than",
    "via", "with the result of", "from inside" or in a relative clause ("A
    calls B, which uses C") is not listed with the callee: it stays
    unchecked. The definitions around the cited lines (a method's class) are
    locators;
  - a verbatim quote verifies only the quoted text: written text that says
    more than a locator (`path:line`, or the name of the definition around the
    cited lines) besides `contains: ...` is `partial` (code `quote_rest`).
- **Scope-exhaustive checks at creation** (`critique.check_at_creation`, run by
  `claim add`; the same checks run in `challenge`):
  - relation: a cited line without the call is checked against the caller's
    whole body (`entail.caller_scope`, AST; direct calls, import aliases and
    `x.name()` on any receiver count). No such call is `contradicted`, with the
    scope printed: "no direct call to save in create_order_handler
    (orders/api.py:16-21); calls through other names are not followed". The
    caller's body is the refuting evidence. A call at another line of the body
    is a heuristic warning (the citation is off), no longer a definitive
    refutation, and so is a caller read from the text whose definition is not
    found (its body was not read), or written text with no caller whose body
    could be read. A caller written as a plain word ("checkout calls submit")
    that names the definition around the cited line is that caller, as if
    written as code: a call elsewhere in its body is the same warning, and a
    body without the call refutes the claim (before, the cited line alone did,
    so a citation one line off contradicted a true sentence). Any other plain
    word is a heuristic doubt only;
  - config: when no read of the variable in the cited file is bound to the
    text's subject and another read's binding spells the whole subject, the
    claim is `contradicted`: "orders/config.py:6 binds ORDERS_MAX_ITEMS to
    MAX_ITEMS_PER_ORDER; DISCOUNT_THRESHOLD reads ORDERS_DISCOUNT_THRESHOLD at
    orders/config.py:7 (scope: environment reads in orders/config.py)";
  - order: `claim add --kind order` records "A before B in F" (a behaviour
    claim, `entail.order_proposition`) from explicit forms only: "A before
    B", "B after A", "after A, B", "A, and after that B" (the anaphor keeps
    the written order), "A, then B", TR "B'den önce A", "A'dan sonra B",
    "önce A, sonra B". Two order words that make no such form, or a negated
    order ("never calls B before A"), are refused with a request for a clearer
    sentence - a guess could reverse a true sentence. F is `--symbol`, or the
    one name the text makes the place or the caller ("in `F`", "by `F`",
    "`F` calls ...", TR "`F` içinde", "`F`'de", the one name without a case
    ending before "çağırır"), never simply the first name ("`validate_items`
    runs before `save` in `place_order`" was checked in validate_items'
    body); otherwise the sentence is refused, and so is an F read from the
    text that is not a definition around the cited lines. The first calls of A and
    B in F's own code decide it (`entail.call_order`); a missing call, or the
    reverse order in a function without branches or loops, is
    `contradicted`. A call inside a def, lambda or class nested in F runs
    when that is called, so it has no static place in F's order: the grade is
    `partial` (code `nested`) and a reversal is only heuristic. The analysis'
    own order claims are graded the same way (they used to verify by word
    overlap);
  - location: a written claim that a Python file defines a name is
    `contradicted` only when nothing in the file binds the name (def, class,
    assignment, import or its alias, parameter, `global`, ...) and the file
    does not spell it at all ("no definition, assignment or import named
    `place_orders` in orders/service.py, and the file does not spell
    `place_orders` (scope: the file's text); nearest: place_order"). A name
    the file spells without a binding is a heuristic doubt. A module or
    class constant is defined by its assignment (`entail.assignment_spans`:
    "`DISCOUNT_THRESHOLD` is defined in orders/config.py" citing line 7 is
    `full`), and `--symbol path::name` names the file.
- **Code-shaped mentions are not substituted** (`question_plan.link_mention`).
  A mention in backticks, a path, snake_case, camelCase, dotted or ending in
  `()` needs a name tier (exact id, path, `Class.method`, label, folded
  label). Without one:
  - if the repository spells it nowhere outside import statements
    (`question_plan.name_site`, a file scan that is lenient on purpose: a
    dotted name counts when its last part occurs, any letter case counts), the
    link is `not_found` with `did_you_mean`. The first unknown reads "no
    symbol named `place_orders` in this repository; nearest: place_order
    (orders/service.py:19)", and the sub-question is `unmet`;
  - if it is spelled somewhere (an environment variable, a data key, an
    external name), the link is at most `weak`, with the site in the
    uncertainty ("no symbol in the index is named `X` (the name occurs at
    ...)"; for a dotted name found by its last part: "(`save` occurs at
    ..., not the whole name)"). The whole name is preferred to its last
    part for 0.25 s after the part is found;
  - a dotted name whose owner is a class or module of the graph
    (`OrderRepository.place_order`, `Cart.check`, `orders.config.X`; a
    package counts with all its modules, a directory - a Go or Java package -
    with every code file in it) is looked for inside that owner, not by its
    last part (`question_plan._member_site`). A Python class's members are
    what it defines (its syntax tree: defs, class-level assignments, the
    attributes its methods assign on `self`); `self.conn.execute(...)` does
    not make `execute` a member. Absent there, the name is `not_found` (with
    the owner's similar members and same-named symbols as `did_you_mean`)
    only when the owner cannot get members from elsewhere - a Python class
    written with that exact spelling, without a base class, a decorator or
    dynamic attributes (`__getattr__`, `setattr`, `__dict__`), a Java class
    (not an interface, enum or record) without a base or an annotation (its
    members are the words of its body; `Object`'s are never absent), or a
    Python module without a star import or runtime names - and the repository gives
    the member nowhere else: not the whole name, not an attribute assignment
    (`Settings.patched = True`) or `setattr`, not a key in a data or
    configuration file (`pricing.discount_rate` with `discount_rate: 0.1` in
    settings.yaml). Otherwise (an owner in another letter case - `cart` is a
    variable or a section, not `Cart` -, a class in another language, a
    directory package, a dunder every object has) the lenient search runs.
    Reading the owner's files shares the 2 s limit of the scan;
  - when the scan could not finish (over 2 s), the link is at most `weak`
    and says that existence was not checked; it is never linked to a
    similar name;
  - a folded label that differs by more than letter case (`placeOrder` for
    `place_order`) is `weak` with "`placeOrder` is spelled `place_order`
    here".
  `name_site` reads `path::name` in that file only and normalises
  backslashes, `Class#method` and `name()`. When the graph's search index is
  current and no indexed file has every word of the name, only the files it
  does not index (or that changed since) are read, so a large repository
  answers "not found" without the 2 s scan.
  `trace` resolves an endpoint to the node it names exactly
  (`retrieval._names_exactly`): a path with or without its extension or with
  backslashes, a dotted module name, `path::Class.method`, `Class#method`,
  `name()`, `Owner.name` with the owner a class, module or package (a Java
  FQN). An endpoint written as code that names nothing exactly is checked
  for existence as analyze checks a mention (`name_site`: a member of a known
  class or module in that owner, a module constant, a name a module imports);
  an owner the graph does not define needs the whole name spelled ("Foo.save"
  is not found although `save` is). Only a name found nowhere is unresolved
  with the not-found line; otherwise the similar node is kept and `fuzzy`
  says so ("`DISCOUNT_THRESHOLD` occurs at orders/config.py:7, not the whole
  name"), as for plain words. Since D37 (section 10) this resolution is
  `naming.resolve`, shared with `map --view impact` and MCP `node_inspect`: a
  detected copy gives way to the project's own code, a name that still names
  several symbols is `ambiguous` (listed, none picked), a name spelled only in
  a file changed since the index is `not_indexed`, and a code name the index
  spells but has no node for (a constant, an attribute) is `not_a_symbol` with
  where it occurs - no longer kept by similarity.
  An analysis stores a claim with its own uncertainties; the question's
  reading (a weak link, an open clarification) is added when the claim is
  shown, so a claim reused by a later question does not carry it.
- **An exclusivity question is answered by an exclusivity check** (review
  2026-09-25: "Is sqlite3.connect only called in repository.py?" was `met`
  with path claims while orders/reports.py also called it). A yes/no
  question with only/solely/"anywhere else" (TR sadece, yalnızca, "başka bir
  yerde") and a call or use verb, naming one call written as code and the
  file(s), is checked (`analysis._exclusive_guard`): an imported module's
  call (`sqlite3.connect`) by the only_in engine of D33 (scope all, tests
  included), a module-level function or class of the project by every call
  edge the index has into it. A call outside the files answers it (a
  `contains:` claim at the site, verified when the engine binds the call; a
  caller from the index is the callers handler's claim); none found is one
  `exclusive` claim at `strong_inference` with the engine's limits, which
  critique re-checks. A method, a call the engine cannot resolve or a
  question without the files is `not_supported` with an unknown that says
  the claims show where it happens, not that it happens nowhere else.

Limits: Python only for relation scopes, config bindings and order (other
languages keep their partial grades). A config or relation claim graded from
source lines in another language is `strong_inference` at most
(`entail._python_only`; analyze asks for no more and says why): a pattern
finds the environment read and the syntax tree the call, but nothing binds
the claim's subject, the enclosing caller or the import scope (senior
evaluation 2026-09-25: "jwtSecret is read from DISCOUNT_THRESHOLD" at a
TypeScript `config.ts:3` was `statically_verified`, and analyze verified a
Java relation that `claim add` graded 0.70). A static resolver's definitive
answer (a SCIP index) still verifies. A call hop of a flow outside Python is
capped the same way (`entail._flow`, and analyze's `_path_claim`): the same
Java call was `partial` as a relation claim and `full` as a flow hop (branch
review, 2026-09-26). Calls through other names, dynamic
dispatch and runtime order under branches are not followed, and the scope
text says so. Word overlap still makes evidence relevant, so generated text
without a typed check stays `strong_inference` and written text
`weak_inference`. The check of the agent's own answer (sentence by sentence)
is a separate, later step.

### 4.3 Name existence (added after round 3)

**D32. Name-existence check** (`verinoda check`, `verinoda api`; `codecheck.py`,
`codecheck_env.py`, `codecheck_facts.py`; needs the `precise` extra).

- Finding: AI-written code imports modules, calls functions, passes keyword
  arguments and reads dict keys that do not exist, or not in the installed
  version. `resolve-call` answered "unresolved" both for a missing name and for
  a receiver of unknown type, never checked imports or keywords, and resolved
  against Verinoda's own interpreter instead of the project's environment.
- Sites: imports and from-imports, attribute loads, keyword arguments, and
  constant keys read from the dict literals a function returns. `--diff`
  checks the sites on changed lines (plus new files); its revision is resolved
  to a commit first, so it is never read as a git option, and its diff sets its
  own prefixes and `--relative` (the user's `diff.mnemonicPrefix`/`dstPrefix`
  settings, and a `--repo` below the top of the work tree, dropped every
  changed tracked file: third review round); a changed file that is not
  checked (a stub) is listed in `incomplete`. `--stdin --as PATH`
  checks code before it is written; the snippet's own definitions stand for
  PATH (a changed signature is judged from the snippet, not the file on disk).
- Environment: `--env PATH`, else `<project>/.venv`, `venv` or `env`, else
  Verinoda's interpreter for the standard library only; third-party names are
  then `not_installed`, never `absent`. Standard-library names come from that
  interpreter itself (`python -I -S`), not from jedi's bundled stubs. The
  report header names the interpreter, the package versions used and lock-file
  mismatches.
- Safety (review of 2026-09-25: jedi's `safe=True` passes any file on Windows,
  where every file's `st_uid` is 0; a `.pth` import line ran on each check; a
  package `__init__` ran when jedi read a compiled submodule): nothing from the
  checked repository runs. A virtual environment's own interpreter is never
  started; the base interpreter its `pyvenv.cfg` names is, with a search path
  built from files (site-packages, `.pth` path lines, `PYTHONPATH`). A `.venv`
  found in the project is used only when that base interpreter lies outside
  the project and is known to the system (Verinoda's own, the Windows
  registry, `PATH`, a Python manager's directory, owned by root); `--env` on
  the command line is trusted as given. The MCP tools' `env` comes from a model
  that the checked repository may steer, so it is held to the rule of `auto`:
  only a virtual-environment directory whose base interpreter is known to the
  system and lies outside the project, never an interpreter path (third review
  round). The note for a `.venv` that was not used names the program `--env`
  would start. jedi imports a compiled module only if it is a
  standard-library module from the interpreter's own directories (a patch of
  `jedi.inference.imports._load_builtin_module` installed by `codecheck_env`,
  limited to the check's jedi project). The oracle imports no `X.__main__`.
- Closed-world rule: `absent` only from a closed container - a module with no
  `__getattr__`, `exec` or `globals()` writes and closed star imports; a class
  object; an instance made by a direct constructor call, or held by a single
  unreassigned local that is not handed to code that sets attributes (slotted
  instances and instances of C types without a `__dict__`, `collections.deque`,
  are closed whatever they are handed to; so is a local bound to a literal,
  `d = {}`); one known signature
  without `**kwargs` or an unknown decorator; the keys of dict literals a
  function returns. A parameter, an annotation or an inferred return value
  leaves the receiver `unknown`. jedi must also fail to find the name.
- Still `unknown` although the container is closed: a name the project
  assigns where it may reach the container - on a module or class name
  (`mod.x = ...`, `setattr(Cls, "x", ...)`), on a variable that holds its name
  (`for c in (A, B): c.x = 1`), on a parameter of a function called with its
  name (`def reg(cls): cls.x = 1` ... `reg(A)`), on the owner in
  `__set_name__`; attributes set by computed name on it (`setattr(mod, k, v)`,
  `mod.__dict__.update`); a name an installed package assigns on a module or
  class it imports (plugins: `pytest.lazy_fixture = ...`); a
  standard-library name this interpreter lacks but the typeshed stubs declare
  under a platform or version condition (`os.fork` on Windows), or that the
  module's own source binds under a condition this interpreter did not take
  (`subprocess.select` on Windows); `sys`
  attributes that exist only in some runs (`sys.ps1`, `sys._MEIPASS`);
  constructor keywords under a metaclass other than `type` (`Color(value=1)`
  for an Enum); a project larger than the file limit (5,000 Python files) -
  its attribute stores were not all read.
- Not closed at all (review of 2026-09-25): a module a module-level call
  changes through the caller's frame or `sys.modules` (anyio's
  `set_deprecated_aliases` installs a module `__getattr__`), or whose
  module-level code writes `locals()`; a class or module described only by a
  stub whose module is compiled (a stub need not list every name); an instance
  whose class has a metaclass other than `type`/`ABCMeta`/`EnumType` (ctypes
  fields); a local instance given to `setattr`/`vars`/`object.__setattr__`,
  or to a C function that may keep it (`list.append`, a queue); `self` put in
  a tuple or list; a call to an Enum with member names (the functional API
  returns a class); an instance (or class) whose class has a method decorator
  or a class attribute made by a call whose code - a descriptor's
  `__get__`/`__set__`/`__set_name__`, a wrapper, a property getter - sets
  attributes on the object it is given, or cannot be read or followed (the
  lazy_property recipe `setattr(self, "_lazy_" + name, ...)`: third review
  round; the standard library's `property`, `functools.cached_property`,
  `staticmethod` and a wrapper that only calls the method keep it closed). For
  a closed standard-library module the interpreter's names are complete: what
  jedi reaches through the module's own imports is not one of its names
  (typeshed's `collections` stub imports `Mapping` for its annotations, and
  `collections.Mapping` was reported as existing). A decorator keeps a
  signature or class closed only when it comes from the module that defines it (`functools.cache`,
  `dataclasses.dataclass`), not by its name. Names that do exist: a
  metaclass's names on the class (`Base.register` under `metaclass=ABCMeta`),
  what a classmethod sets on `cls`, mangled `__x` names, `typing.Protocol`'s
  own names, a package's submodules its `__init__` imports (for star imports),
  `field(init=True)` in a dataclass, keywords of every conditional definition
  of a function (`if sys.version_info ...: def f(a) else: def f(a, b)`).
- jedi answers from outside the project, the environment's search path and
  the standard library are ignored: jedi's own process has `jedi` and `parso`
  imported, which would otherwise make them "exist" in any environment.
- Guards: try/except ImportError (AttributeError, TypeError, KeyError for the
  other kinds), `if TYPE_CHECKING`, version and platform tests, feature flags
  (`HAS_X`, `IS_X`, `PY3`, `X_AVAILABLE`), `hasattr`, and a
  `getattr`/`hasattr` test of the same receiver
  (`if getattr(sys, "frozen", False): sys._MEIPASS`) make a missing name
  `guarded`. A broad handler (bare `except`, `Exception`) guards an import
  only: an attribute, keyword argument or dict key inside `try: ... except
  Exception: log` stays `absent` with `swallowed_by` naming the handler that
  would hide the error at run time (senior evaluation 2026-09-25: a request
  handler's `Path(...).read_json()` and `ThreadPoolExecutor(thread_prefix=)`
  were `guarded`, exit 0); a flag the module binds once to a constant
  (`IS_PROD = True`) tests nothing (second review round). pytest's
  `pythonpath` option adds its directories to the search path; a
  `conftest.py` above the file (or the file itself) that changes `sys.path` or
  `sys.modules` - in any form: `insert`/`append`, `+=`, a slice, an alias of
  `sys`, `from sys import path`, `site.addsitedir` - or the module in a
  plain (non-package) directory of the project off the assumed search path
  (`lib/helpers.py`), makes a missing top-level module `unknown`, never "not
  found in this project"; a file inside a package does not count
  (`pkg/extractors/robot.py` is not `robot`).
- Constructors: a standard-library base with no `__init__`/`__new__` of its
  own (`abc.ABC`, a mixin) does not answer for a class's keywords; the next
  class in the MRO does (`class Plugin(abc.ABC, Base)` takes `Base`'s). When
  such a base's constructor comes from one of its own bases and another base
  follows it, the keywords are `unknown` (the real MRO may put that base
  first). Keywords are judged against the class object (its `__new__`,
  `__init__` and metaclass), not against what may later be added to an
  instance (`threading.Thread(deamon=True)` is absent).
- Python only, and said so: a file in another language that was named, lies
  under a directory named, or changed in the diff (`Foo.java`, `x.ts`, a
  snippet `--as Foo.java`) is listed under `not_checked` with its language,
  never parsed as Python and never counted as checked; the result's `status`
  is `unsupported_language` when nothing else was checked, `incomplete`
  otherwise (senior evaluation 2026-09-25: `check EmberForgeBlockEntity.java`
  said "0 sites in 0 files", exit 0, and a TS rename passed `check --diff`).
  A notebook (`.ipynb`), Cython (`.pyx`/`.pxd`) and the other languages the
  index reads (shell, PowerShell, Objective-C, Fortran, Verilog, ...) count
  as other languages; a Python file that does not parse or cannot be read,
  or a snippet that does not parse, is listed too and not counted under
  `files`. Exit codes: 3 when a name is absent or an installed version
  differs from the lock (fix the code or the environment), 4 when nothing of
  that was found but something asked for was not checked (another language, a
  file that does not parse, the walk limit, the MCP time budget), 0 when
  everything asked for was checked; `exit_because` names both. `api` on a name
  of the project's code in another language is `decided:
  unsupported_language`, exit 4. (The branch first used 3 for both; a review
  showed that a CI gate or hook could then not tell an invented name from a
  JavaScript file next to the Python, so `check verinoda/ui` could never
  pass.) A diff with no changed `.py` file is `nothing_to_check` (exit 0)
  with a hint to compare with the base branch in CI. The help, the MCP
  descriptions and the skills say "Python only".
- Output: nearest real names (edit distance with transpositions, shared word
  parts, a few synonyms) and where the name is defined elsewhere (the
  project's, or the installed package's, functions, classes, variables and
  methods; found through a word index of the files' text built once per call
  on the first absent name, so only files that contain the name are parsed -
  the same answers as parsing every file, which made a diff with an absent
  name about 10x slower than a clean one). Wording:
  "not found in <container> as installed in <env> (<file>)", never "does not
  exist". Exit 3 when something is absent or an installed version differs
  from the lock; `exit_because` says which. `incomplete` lists what was not
  checked (the file limit, the MCP tool's 90-second budget, files that could
  not be read or parsed). `api A.B.C`
  looks up every part: the attributes of a function or variable are not
  listed. `api` says `found: false` (exit 3) only where `check` would say
  `absent` (a closed container, the same exceptions) or for a module that is
  not on the search path; an answer it could not decide is `found: null` with
  `decided: unknown` or `not_installed`, exit 0 (third review round).
- Cache: per file in `.verinoda/cache/check/`, keyed by the file's sha256 and
  the environment fingerprint; an answer is dropped when a file it was read
  from changes (project files, and files outside the project and its
  site-packages such as an editable sibling), or the set of project files
  changes. The files read are every file jedi loaded to answer the file's
  sites - each step of a re-export chain (`pkg/__init__` -> `pkg/api` ->
  `pkg/old`), not only the final definition - and the sources of star
  imports; if jedi's module cache cannot be read, every project file. A
  long-lived process (the MCP server) starts other files' jedi scripts afresh
  on every call, since their inference keeps the modules they imported. A
  new or removed package in the environment (also an explicit `--env` in a
  long-lived MCP server) changes the fingerprint. Past 5,000 project files the
  cache is off. A `--diff` answered from the cache selects the same sites as a
  fresh run (a call's keywords by the call's lines). Import answers also
  depend on every `conftest.py` from the file's directory up to the project
  root (recorded also where there is none, so that a new one drops the answer)
  and on pytest's `pythonpath` (part of the key); a long-lived process rebuilds
  its jedi project when `pythonpath` changes (third review round).

## 5. Delivery plan

1. **Round 3, in parallel on disjoint files:**
   - search engine (D17–D22);
   - question understanding (D1–D9);
   - trust engine (D23–D27, D30);
   - runtime and precise resolution (D28–D29);
   - reference resolver (D10–D16).

   The schema v3 migration is written first, once, for all tracks.
2. **Integration:** analysis wiring, CLI (`plan`, `resolve`, text output), MCP
   tools, skills (understand-first protocol).
3. **Measurement:** benchmark schema 2 with held-out, Turkish, compound and
   version-bound variants, budget sweeps, and the staleness and critique
   harnesses. Numbers are published only from result files.
4. **Adversarial acceptance audit:** fix what it finds, then push.

## 6. Decisions the human makes (D33)

**Finding.** A question about a future choice ("should we move orders from
SQLite to PostgreSQL if traffic grows?", "hangi veritabanını seçmeliyiz?") was
read as a flow or dataflow question and came back `met`, with path claims that
do not answer it. Nothing enforced a recorded decision either: a new module
calling `sqlite3.connect` passed `notes`, `challenge` and `verify`.

**D33.** Verinoda never chooses. For a decision it collects evidence, asks,
records what the human chose and checks the code against it.

- *Intent.* `decide` is a plan intent. Strong cues ask for a choice or a
  recommendation in so many words (EN: "should we use/switch/...", "would you
  recommend", "the right choice", "a better fit", "overkill", "is it worth
  / time to", "X or do we need", "what should our X be", "enough for us";
  TR: seçmeli-, hangisini kullan/seç, "iyi bir fikir mi", "önerirsin",
  "yeterli mi", "yüke dayanır mı", a first-person "büyütürüz"). The other
  cues (a clause that opens with "should", "X or Y" between two known
  technologies, "do we need a", "-meli miyiz", the optative "-alım/-elim",
  "geçsek mi", pros and cons of) count only when no veto fires: a past
  tense ("why did we decide", "yazmışız"), a question about what the code
  does ("how does X migrate", "what happens", "nasıl/nerede", a present
  "-iyor"), a usage verb ("hangi testi çalıştıralım"). A growth condition
  ("if traffic grows", "sayısı artarsa") is never a decision on its own. A
  decide cue outranks every other intent in a clause. A sub-question another
  intent answers (a host agent's plan) whose words carry a strong cue gets the
  brief and the same verdict, its claims kept as context; words that only may
  ask for a choice ("best", "smarter") get a note, and the verdict is at most
  `met_with_inference` (review round 3: a note alone let such a question end
  `met` with data-path claims). Review round 3 also added the strong cues
  "would you pick", "worth the effort", "get away with", "right time to",
  "fits best", "keep X or replace", "iyi olur mu", "tam zamanı mı", and the
  question particle with a personal ending ("miyiz", "misin", "muyuz") as a
  Turkish question word, so "gecmeli miyiz" typed without Turkish letters is
  read as Turkish and "... nerede açılıyor ve ... geçmeli miyiz?" splits at
  "ve". A `decide` sub-question's `done_when` kind is `decision_brief` and its
  verdict is always `human_decision_required` (`question_plan.HUMAN_DECISION`),
  never `met`, whatever claims exist. analyze runs no retrieval for it (the
  options a decision names need not exist in the code, so "these words occur
  nowhere" is not reported) and routes it to the decision handler. Two things
  it does not answer are said (review 2026-09-25): a name written as code
  (dotted, snake_case, `name()`) that is no known option and does not exist
  here keeps its "no symbol named" unknown, and a question about what the
  code does in the same clause (how does, nasıl, nerede) gets an unknown that
  it was not answered. The brief lists the options the project uses apart
  from the ones the user named ("options: Redis; the project uses: SQLite").
  "yeterli mi" that judges a name written as code and names no technology,
  "us" or load ("validate_items boş siparişi reddetmek için yeterli mi?") is
  read as English "is validate_items enough to ..." is: its claims and a
  note, not a decision. A Turkish comma splits two clauses when each side
  asks its own question (an intent cue and a question word or particle, or a
  decision cue: "SQLite yeterli mi, place_order siparişi nasıl
  kaydediyor?"), unless the left side is a condition, the right one an
  alternative ("..., yoksa PostgreSQL mi?") or both sides are the options of
  one choice ("Fabric'e mi geçelim, NeoForge'da mı kalalım?"). On the 491
  questions of the benchmark results and tests, only the review's cases and
  "..., yoksa ..." choices (now one clause) are read differently.
- *Records.* A decision is a Markdown file with a front matter
  (`verinoda-decision: 1`, id, status, `decided-by: human`, supersedes,
  governs, guards, revisit-when, waivers) in the decisions folder (default
  `.verinoda/decisions/`, which git does not see; a committed folder for CI).
  The folder is, in order: `--decisions-dir` (`decide check`, `decide list`),
  `decisions.dir` in `.verinoda/config.json`, `[decisions] dir` in a committed
  `verinoda.toml`, `[tool.verinoda.decisions] dir` in `pyproject.toml`; `decide
  check` prints which (senior evaluation 2026-09-25: with the folder set only
  in the git-ignored config, a fresh CI clone reported "0 violated (0 decision
  records)", exit 0, over a committed violation).
  The file is what is checked; every event (record, import, guard, accept,
  waive, supersede) also appends the whole state to the `decisions` table
  (schema v5, append-only), so a hand edit is visible. `decide record` needs
  the chosen option and the rationale in the human's words; MCP
  `decision_record` refuses record/guard/accept/waive without the user's words
  (`user_statement`). A hand-written ADR is never edited: `decide import` makes
  a companion record whose guards are only *proposed* (from sentences with
  only/must/never that name code the index knows) until the human accepts
  them. A record that says `decided-by` anything but `human` is reported and
  not enforced. So is one with a header line that is not one-line `key: value`
  (a YAML block list of guards would otherwise be enforced with no guards), a
  list entry of the wrong type (`allowed` written as a string), or an id another
  record file also uses; a record another enforced record supersedes is not
  enforced even when its own file still says accepted (a merge). A waiver whose
  `until` is not a `YYYY-MM-DD` date is not applied and is listed.
- *Guards* (`verinoda/guards.py`, one rule engine). `only_in` (a call may
  appear only in the allowed files; product code by default: tests,
  reference trees, detected copies and the decision folder are out of
  scope; `conftest.py` is test code; example, sample, demo, fixture and vendor
  folders and generated files - a `*_pb2.py` name, or a head that says
  generated and do not edit - are out of scope too and named in the limits;
  `scope=all` keeps them all. Such a folder counts only above a source root
  (`src/<set>/java|kotlin|...`) or outside the path a JVM file's `package`
  line names: `com/example/...` is the Fabric template's package, and an
  only_in guard over it checked no file and said ok - senior evaluation
  2026-09-25): Python names are resolved by scope as
  Python does (module, function, lambda, class and comprehension scopes,
  `global`/`nonlocal`; a binding under if/for/while/try/except/match may not
  run, so the last unconditional binding and every conditional one after it
  can reach a use; a function a module-level call may run before a later
  rebinding also sees the import or alias bound before that call). A call is
  VIOLATED when every binding that can reach it
  is the target through imports, aliases, simple assignments and re-exports
  through the project's own modules; POSSIBLE when only some are
  (`except ImportError: psycopg2 = None`), when a parameter, loop, with,
  except or comprehension variable shadows the import, for the target used
  as a value (`functools.partial`, a class attribute), `getattr(m, "f")`,
  `importlib`/`__import__` and star imports; a def, class or literal that
  replaces the import is not the target. Java/Kotlin calls are VIOLATED when
  the class is import-bound (static call, or a receiver declared with the
  class - or its import alias - anywhere in the file, and never bound without
  a written type: a lambda parameter or `var` of the same name keeps it
  POSSIBLE), otherwise POSSIBLE; other languages are a regex over the code
  with comments and strings removed, POSSIBLE at most. `no_edge` reads graph edges: an
  EXTRACTED edge whose cited line still names the target in code is
  VIOLATED, an INFERRED one POSSIBLE. A `from` or `to` that matches no indexed
  file (an external package such as `net.minecraft.client.**`, which is no
  node) is `unknown` with a hint (only_in for calls into a package), never
  ok. No edge out of the `from` files is `ok` (edges_checked 0, with a limit)
  when the index has edges of the guard's relations out of other files of
  their language (a leaf module such as `constants.py` was looked at), and
  `unknown` when it has none for that language anywhere (the extractor may not
  emit them). `dependency absent|present` reads the
  root manifests, the package.json of every workspace package the root
  declares (`workspaces` in package.json, `packages` in pnpm-workspace.yaml;
  the globs are matched as npm, yarn and pnpm match them - `*` within one path
  segment, `**` across them, `./` and `{a,b}` understood - and a declared
  package is read whatever its folder is called, `packages/build` too; a
  declared glob that matches no package.json is named in the limits),
  plus the root Gradle/Maven build and the subprojects it includes, with the
  version catalogs in `gradle/*.versions.toml` (`implementation(libs.x)`,
  bundles and `alias(libs.plugins.x)` are cited at the build line, with the
  catalog line); `include`/`jarJar`/`shadow` lines are cited too (from the
  project's file list: git-ignored files are not read;
  comments - `//`, `/* */`, `<!-- -->` - are blanked first, so a commented-out
  dependency is not declared);
  build files under test, sample, fixture or vendor folders are skipped and
  named in the limits (so are other manifests under such folders), another
  build is POSSIBLE, other manifests (not the
  root's, a workspace package or an included build) are listed as not read; a
  Maven item is cited at its `<artifactId>` line, one finding per cited line.
  Every file read counts as a manifest, whether or not it declares anything;
  with none read, `absent` is `unknown` (it was `ok (manifests 0)` for a
  Kotlin-DSL build with a version catalog). `governs` compares
  the symbol's anchor fingerprint: REVIEW, never VIOLATED. `revisit-when` fires TRIGGER once its
  condition starts to hold. Every `ok` states its scope - what it counted
  (Python/JVM/text files; from_files, to_files, edges_checked; manifests;
  files) - and its limits; a guard that checked no file, edge or manifest is
  `unknown`, never ok. `decide check` exits 1 on VIOLATED, 3 when nothing is
  violated but something was not checked (status `unknown`: such a guard, a
  file that could not be read or parsed, a record that cannot be read, a
  decisions folder that is configured - flag, config, `verinoda.toml`,
  `pyproject.toml` - but does not exist, or no
  record while the repository holds ADR-like files - Markdown under
  `adr`/`adrs`/`decisions` or with a `verinoda-decision` front matter, outside
  sample and test folders, not a folder's README, index or template; the
  message names them and the ways to set the
  folder), and 2 on an error (with `--json`, also as
  a JSON object); its status is `ok` only when every guard was checked - a guard
  or file that was not (a file that does not parse; a byte-order mark is fine),
  or a record that cannot be read, makes it `unknown`, and violations only in
  unchanged files make it `pre_existing`, each with a next step; with `--base REF` /
  `--changed` only new/touched violations count: a finding is new when its
  file or a file its binding passes through (a re-export module, an edge's
  target) changed since the base, else it is listed as pre-existing - which
  says those files are unchanged, not that the base tree was checked. The
  changed files are read project-relative (`--relative`) and NUL-separated
  (`-z`), so a project in a subdirectory of its repository and non-ASCII
  paths count. A record whose front matter or an entry of it cannot be read
  (a missing id or file, a BOM is fine) is listed as not enforced and never
  rewritten by verinoda; the other records are still checked. A ref is
  refused when it starts with `-` and is resolved with `rev-parse --verify
  --end-of-options`; `--` precedes paths.
- *Brief* (`verinoda/decision_brief.py`). No recommendation field and no
  score. `forces` are facts from probes P1-P9, each with evidence that
  re-checks (a line and the text that must be on it); a force without
  evidence cannot exist - what was looked for and not found is an `absence`
  with the globs or patterns searched and its scope ("no Dockerfile in the
  repository", never "not containerised"). Only forces and absences that
  serve the decision's kinds (datastore, dependency, boundary, scaling,
  other; derived by rules) are kept. `options` (named in the question, by
  `--option`, or used by the project) carry presence evidence (declared
  dependencies, imports read from the syntax tree - `import os, sqlite3`
  counts, a docstring example does not - and a connection the engine bound to
  the option's driver; a connection through an engine that does not name the
  database, such as SQLAlchemy's `create_engine` or JDBC's `DriverManager`,
  makes a datastore option's presence unknown with that reason, never "not
  present"), the code a
  change touches, installed metadata (read from the project's venv, never
  imported) and external claims only as quote-checked pins: the page is
  fetched through `research` as `research.network` allows and the quote must
  occur verbatim, else it is dropped (`unknown` without the network); even
  then it says what the page says, not that it applies here. The agent's own
  arguments are `weak_inference`. `questions_for_human` come from fixed
  EN/TR templates per kind, at most 5, each with `asked_because` (and
  `asked_because_tr`; a force it cites keeps its status, e.g. "appears to keep
  one instance per process (strong_inference)") and
  `discriminates`; no rule can show that a file answers what the human
  expects, so a file that bears on a question (a Procfile, a compose file
  with a database image, a retention setting) is attached as
  `partly_answered_by` and the question is still asked. Probes read code
  without comments and docstrings; a module-level instance is called shared
  only when the code sets it once (`if _repo is None`), and then as
  `strong_inference`; an ADR's reason is read only from the paragraph that
  states the decision; an argument or quote that names no option is kept
  under `not_tied_to_an_option`.
  Answers are appended with `answered_by = user` and go into a record's
  Context marked as the user's; they support a decision's rationale, never a
  claim about the code.
- *What stays human:* choosing between options; load, growth, SLO, budget,
  hosting, team and compliance facts; whether a guard proposed from prose means
  what the record meant; waivers; superseding a decision.
- *Limits.* The cue tables are written by the rule authors. On two held-out sets
  of 10 questions (written and hashed before the rules they measured) the
  builder's frozen rules had precision 1.00 and recall 0.40 each. The review
  found 15 look-alike code questions read as decisions and 11 missed decisions
  on 44 questions; after the review fixes the one set still held out (20
  questions, hashed before those rules were written) gave precision 0.83 and
  recall 0.50: decisions are phrased in many ways the tables do not know
  ("what would you pick", "fits best", "doğru zaman mı"), and a missed one is
  still judged by the intent it was given (a note says it may ask for a
  choice when its words suggest one). A host agent that writes the plan can set
  the intent itself; the rules are the fallback. Review round 3 wrote 20 new
  choice questions and 20 look-alikes before running the router: precision
  1.00, recall 0.55 (two choice questions ended `met`, one Turkish one typed
  without Turkish letters ended `met` on unrelated claims). The cues added for
  them (above) make that set in-sample (20/20, no look-alike read as a
  decision). Held-out 4 run again with them: precision 0.86, recall 0.60 - but
  its one new hit ("what would you pick") is a phrasing this section had named
  from its misses, so it is no longer a clean held-out set, and no clean one is
  left. A missed choice question whose words may ask for a choice is at most
  `met_with_inference`; one with none of those words can still end `met`.

## 7. Debugging loops (D34, 2026-09-25)

### 7.1 Findings that drive the design

- An agent fixing a bug through `verinoda experiment run` produced four unconnected runs: nothing
  recorded which code each ran on (the CLI passed no commit; evidence `commit_sha` was NULL), two of
  them ran byte-identical trees, and the exception was only in `stdout.txt`.
- Typical loops: editing the test instead of the code, masking the error (`.get(k, 0)`), reverting to
  a state already run, the error moving while the failing tests stay, the same fix idea retried.
- A differential run of the repro on the base commit, in a copy, pointed at the real cause in one
  step in the prototype.

### 7.2 Decisions

- **Tree identity** (`treestate.py`, cross-cutting): content id = sha256 of the CRLF-normalised
  bytes; tree hash over (path, id). `experiments.run` computes it while copying and records it on
  every run (`result.tree`, evidence `meta.tree_hash`). An attempt stores only the files that differ
  from the session base (`{path: id | None}`), their contents by id (`runs/blobs/`) and a patch
  (`runs/<attempt>/change.patch`); any two recorded trees can be diffed exactly. Hunks are mapped to
  the definitions they touch with `anchors.enclosing`.
- **Commit copies**: `experiments.run(ref=...)` copies the regular files of one commit (raw blobs
  through `git cat-file --batch`: no smudge filters run, unlike `git archive` with filters configured;
  symlinks and submodules are left out) under the same policy and isolation, optionally with
  working-tree files laid over it (`overlay`, recorded). The user's tree, index and `.git` are only
  read: git is read with plumbing (`diff-index`, `diff-tree`, not the porcelain `git diff`, which
  rewrote `.git/index` for stat-dirty files even with `--no-optional-locks`). Refs from users or agents
  pass `rev-parse --verify --end-of-options` and are refused when they start with `-`. Paths from
  history that a file system may fold to `.git` (`.GIT`, `.git.`, `git~1`, NTFS streams, Unicode HFS+
  ignores, also between backslashes, which Windows reads as separators: `.\.git\config`) or `.verinoda`
  are never written, and an overlay path is checked the same way and may not go through a symlink; a
  file this OS cannot hold (`what?.md`, `NUL`, any name with a backslash on Windows) is left out of the
  copy and named in the run's `source.skipped` and limits. A project below its git top level works on
  its own subtree (`rev-parse --show-prefix`; commit copies hold only that subtree). A tracked
  symlink checked out as a plain file (`core.symlinks=false`) is not a change. A run of a commit copy
  attached to a claim only qualifies it, unless it is the claim's own commit without overlay.
- **Failure signatures** (`failsig.py`): the `verinoda_failsig` pytest plugin (standard library only,
  loaded next to the copy like the call tracer) records the exception, message, crash location and
  in-repo traceback entries; regex parsers cover pytest text, Python tracebacks / unittest,
  JUnit / Gradle / Maven, Go, Rust and Node. The crash symbol is the innermost in-repository frame,
  mapped to `path::Qualified.name` by `anchors.enclosing` (a Python file with a syntax error: by
  indentation). JVM chains use the root cause (`Caused by`). Messages are normalised for addresses,
  JVM identity hashes, UUIDs, hex ids and hashes, long ids, durations, timestamps, ports, pids and
  copy/temp paths - also in their `repr()` form with doubled backslashes, and anything under the
  run's own throw-away directory (whose `_home` is the run's HOME/TEMP). `sig_exact` = (test, exception,
  symbol, message); `sig_coarse` = (exception, symbol). A failing run without a complete record is
  `unknown`/`partial` and has no keys.
- **Loop rules** (`looprules.py`), each citing its attempts. Definitive (set `stop`):
  `tree_reverted`, `signature_recurred`, `no_progress`, `test_edited`, `failing_tests_skipped`,
  `off_path`. Heuristic (never stop): `file_reverted`, `error_moved`, `masking`,
  `hypothesis_repeated`, `possibly_flaky`. `flaky` suspends all of them and their `stop` except the
  two test rules. Budget: `debug.max_no_progress` (3) fix attempts in a row without measured progress
  also stops, flaky or not. On a *passing* attempt only the two test rules stop.
  Interpretations of the design, fixed before the benchmark ran (with the review changes of 6.5):
  - `tree_reverted`: the whole tree, or the *code* (Python files compared by their syntax tree
    without docstrings: comments, docstrings and formatting aside), is back to an earlier attempt's
    after being different in between. One file back at a content an earlier *fix attempt* introduced,
    with the rest of the code new, is the heuristic `file_reverted` (review: undoing one's own last
    edit while fixing another file is not a loop); a file going back to its starting content (attempt
    0 or the base) is an undo, not reported. A rerun of the same tree is not a revert.
  - `signature_recurred` needs A, then a different *known failing* signature B, then A on another
    tree (the same tree is `tree_reverted`).
  - `no_progress` counts fix attempts only (not the baseline, probes or reruns), and only since the
    last passing attempt (a flaky test is not three attempts without progress).
  - `test_edited` concerns existing test files with replaced or removed lines, or with added lines
    that switch a test off (a skip/xfail marker, `pytest.skip(`, an early `return` inside a test
    function), or a test configuration whose added lines change the selection (`addopts`, `-k`,
    `--deselect`, `collect_ignore`, `testpaths`, Jest's `testPathIgnorePatterns`, ...). A test file is
    one by the shared rule of `verinoda/testcode.py` (Python, `x.test.js` / `x.spec.ts`, `x_test.go`,
    `FooTest.java`, `src/test/` and `src/gametest/`, ...) or the file a failing test of the session lives in. Adding a new test, only adding lines to one (a print, a comment), or
    a change of formatting alone (the file's syntax tree unchanged; an assertion line whose statement
    is the same after parsing) is not flagged. "An assertion or expected value" = a removed/changed
    line matching assertion forms (`assert`, `self.assert*`, `expect(`, `assertThat`, `assert_eq!`,
    `t.Errorf`, `pytest.raises`, `expected =`/`want :=`, ...). It stops even a passing attempt (a test
    edited until it passes is the case to ask about). A file the attempt put back to the content it had
    at attempt 0 is not edited: `git checkout` of a test an earlier attempt changed undoes a test edit
    (senior evaluation: the real fix after such a checkout was stopped). Back to the base commit counts
    only where attempt 0 had the base content too; a test the session started with, uncommitted, is the
    test, and putting it back to the commit discards it (still `test_edited`).
  - `failing_tests_skipped`: a test that failed at the baseline (or at the previous attempt, if it
    existed at the baseline) is skipped, xfailed, deselected or not collected in this run (the
    plugin's per-test outcomes; unknown without them). It stops even a passing attempt, and such a
    pass is reported as "exited 0, but N earlier failing tests did not pass".
  - `off_path` reads the call trace of the repro run itself (`debug start --trace`, `debug try
    --trace`, or the `observe` strategy, which is a traced probe of the repro), complete traces only,
    and only when every edited item is a function or method in a non-test Python file; the trace must
    be taken with the edit in the tree. It needs the edited functions to be called *nowhere* in the
    run - not by any test, not at import/collection time - and no process-starting call
    (`subprocess.*`, `multiprocessing.*`, `os.system`, ...) seen in the run (review: import-time code
    and child processes reach the failing test without being on its call path).
  - `flaky` (one tree and command: another outcome, another set of failing tests, or another coarse
    signature; the message is not compared) is cleared only by a rerun series (3 or more) of a tree
    whose recorded runs all agree; a series on the tree that disagreed never clears it. Found on the
    flaky control (see BENCHMARKS.md). An agent-reported run does not count for a tree Verinoda ran.
    Two different trees with the same code and different results are the heuristic
    `possibly_flaky` (rerun proposed first).
  - Progress between two runs of the same tree or code with different results is `unknown`, and so is
    a run after a test was edited or failing tests were skipped.
- **Strategies** (proposed on `stop` or `flaky`, in the design's fixed order; run on request;
  recorded as attempts): `rerun` first when flaky; `differential` when the tree differs from the base
  and the base is not known to fail; `bisect` when the base is known to fail (a clean baseline, or a
  differential that failed) - both ends are established first (the bad end, default the session
  base, must fail and the good end must pass: a run recorded in the session on that commit, or a
  clean baseline for the base, counts; otherwise it is run; if an end does not behave, bisect says so
  and stops with `unknown`), then first-parent binary search in commit copies, commits that cannot run
  are skipped, without `--good` Verinoda steps back 1, 2, 4, ... commits, and the conclusion cites the
  attempt that shows each side; `observe`; `narrowing` (a suspect list: traceback symbols and symbols
  changed since the last passing state - a pass with `test_edited` or `failing_tests_skipped` is not
  one, else the changed code drops out behind the edited test - each with its evidence; only a
  complete trace in which a changed function was called nowhere, with no child process started,
  rules it out);
  `minimal_repro` (the repro narrowed to the failing tests; when a test passes alone and failed in
  the full repro on the same tree, the attempt reports the heuristic `order_dependent`); `ask_human`
  when a test rule fired or nothing else applies.
  The differential holds the symptom's test fixed: when the files of attempt 0's failing tests differ
  at the base (a new or changed test), the working tree's versions - if still attempt 0's - are laid
  over the base copy (`--overlay` names files explicitly); if the failing tests did not run at the
  base (per-test outcomes), the result is `inconclusive`, never "the cause is in the diff". It ranks
  hunks: code before test files, and files whose code is unchanged (comments/docstrings only) last;
  while the failure still shows the symptom the session started with (same coarse signature or the
  same failing tests as attempt 0), hunks already there at attempt 0 first (found on L6), then on the
  failure's traceback, reached by the failing tests in a complete trace, others; when the failure has
  changed, the traceback/reach tier first (review: the agent's own new bug on the traceback belongs
  above an unrelated earlier change). For a command Verinoda may not run (Gradle, Maven)
  `differential --prepare` writes a plain copy of the base under `.verinoda/runs/` and the agent
  reports its run there (`--kind differential --observed-output ... -- <command>`).
- **Honesty**: never "fixed"; a pass is "the repro command passed at tree T in run R" plus
  `not_run` (tests outside the selection, `-k`/`-m`/`--deselect`/`--ignore`, skipped and xfailed
  tests, other environments, agent-reported) - and only for the session's own repro command: a
  narrowed or other command's pass is reported as that command's and never resolves the session.
  Closing as resolved needs a pass of the repro command whose tree hash equals the current tree, in
  which the tests that failed before passed (not skipped), with no run of that tree by Verinoda that
  did not pass (flaky), and - when tests changed between attempt 0 and that tree - `--accept-test-edit`,
  the user's decision, recorded in the close note. Agent-reported runs must name the command they
  ran, are labelled, their output sha256 kept, their evidence type `agent_report` is non-verifying,
  and they never make a tree Verinoda ran flaky or resolve it. Verinoda never edits or reverts the
  user's files.
- **Surfaces**: CLI `verinoda debug start|try|status|diff|close|differential|bisect|rerun|observe`
  (exit 3 when the ledger says stop, a command is refused, or a strategy did not settle it: bisect
  unknown/open, differential inconclusive, rerun flaky), MCP `debug_start`, `debug_attempt`,
  `debug_status`, `debug_strategy` (with `overlay` for differential and bisect) and `experiment_run`
  (a command passes through as given: repeated and empty arguments are kept), skill protocol in both
  skills. Deviation:
  the design asked to add `experiment run` / `debug` to Claude's `allowed-tools`; only the read-only
  `debug status` / `debug diff` are pre-approved, because the others run the project's tests and
  experiment runs already keep the user's permission prompt (tests/test_agents.py).
- **Schema v6** (v5 holds the decisions tables, D33): `debug_sessions` (symptom, command, base
  immutable; a closed session stays closed) and `debug_attempts` (append-only).

### 7.3 Measurements (docs/BENCHMARKS.md, Update 2026-09-25: debug ledger)

- debugloops_v1, 12 scripted sessions (8 looping, 4 controls) over orders_app and glow_mod copies,
  written with gold before the rules ran. First run: definitive precision 10/10, loop recall 7/8, 0/4
  controls stopped, top strategy 7/8. After three fixes found on these sessions (JVM identity hashes
  in messages; differential ranks attempt-0 hunks first, by line; flaky clearing): 11/11, 8/8, 0/4,
  8/8. In-sample; the sessions and their gold are the builder's. After the review fixes (6.5), run 8:
  11/11, 8/8, 0/4, 8/8, cause named 7/8 - every attempt's findings and stops as before (run 7, with a
  first ranking fix, named 5/8).
- Signature parser: 30 log fixtures (21 real runs, 9 hand-written for tools not installed here:
  Gradle, Maven, Go, Jest): 18/30 exact on the first run, 30/30 after format fixes (in-sample); 10
  held-out real logs: 8/10 on their first run, 10/10 after two fixes. Zero crashes; a property test
  feeds arbitrary text.
- `debug try` overhead beyond the command, committed code: orders_app median 0.14 s, p90 0.25 s,
  max 0.49 s (20 tries on a shared machine); with the tracer median 0.15 s. A 2,341-file tree: median
  5.0 s, dominated by copying the tree per run (threaded copy: 1.8 s vs 3.0 s sequential for the copy
  alone). After the review fixes: orders_app median 0.08 s (p90 0.11 s), the clone median 2.4 s (a
  less loaded machine; the copy was not changed); a 17,504-line changed lockfile 4.7 s -> 0.13 s.

### 7.4 Not done / limits

- `narrowing`, `order_dependent`, the `observe` report (which failing tests reached each edited
  function; the observed call chain from the first failing test to its crash symbol) and the
  differential trace (`differential --trace`: the failing tests' observed calls at the base vs in the
  failing tree) were added after the benchmark and are covered by tests only.
- A real agent session with and without the protocol; the container isolation path; Gradle/Maven
  runs (agent-reported runs only).
- A copy per run makes big trees slow; reusing a per-session copy synced by content id would remove
  most of it and is not built.
- Heuristic rules have no gold labels: in the 12 sessions they fired 15 times (error_moved 7,
  hypothesis_repeated 6, masking 2), 0 times on the controls; their precision is not measured.
- Rust `#[cfg(test)]` modules inside `src/` are not recognised as tests (a test edit there is not
  `test_edited`); per-test outcomes (`failing_tests_skipped`, the differential's check that the failing
  tests ran at the base) exist for pytest only - other runners report "unknown" there. Process
  detection for `off_path` sees only calls made from repository code.

### 7.5 Review of the ledger (2026-09-25) and what changed

Two reviewers reported 27 findings (25 distinct) against the built ledger; every one was reproduced
on the code as built, fixed, and covered by a regression test (tests/test_debug.py,
test_looprules.py, test_treestate.py, test_failsig.py). The false results, by kind:

- **False "passed" / false resolution**: a narrowed command's pass was "the repro command passed"
  and closed the session; skipped / xfailed / early-returned / deselected failing tests counted as a
  pass; an agent report outweighed three failing Verinoda runs of the same tree; status and close
  ignored later failures of the same tree. Now: only the repro command's pass counts, the new rule
  `failing_tests_skipped`, `test_edited` for switched-off tests and test selection, agent reports
  must name their command and never outweigh Verinoda's runs, close refuses flaky trees and asks for
  `--accept-test-edit`.
- **False stops**: undoing one's own last edit while fixing another file (`tree_reverted` per file ->
  heuristic `file_reverted`, and only the test rules stop a pass); `off_path` for import-time code
  and child processes (now: called nowhere in the run, no process started); `test_edited` for a
  quote change (now compared by syntax tree).
- **Missed loops**: temp paths in `repr()` form, timestamps and hex ids made every run's signature
  new, so the tree looked flaky and even the budget was suspended (normalised; flaky is judged on
  outcome, failing tests and the coarse signature; the budget holds while flaky); a flaky test was a
  definitive `no_progress` (the rule no longer spans a pass; same-code flips are `possibly_flaky` with
  rerun first).
- **Unverified conclusions**: bisect named a first failing commit with zero runs, or with a `--good`
  that failed (ends are run first now); the differential said "the cause is in the diff" for a new
  test that the base lacks (the symptom's test is held fixed, else inconclusive); the ranking put an
  unrelated attempt-0 docstring edit above the agent's new bug on the traceback.
- **Safety and robustness**: MCP de-duplicated command arguments (`-p a -p b` lost a `-p`); a
  project below the git top level saw the whole repository as changed; commit copies wrote a
  `.GIT/config` from history (case-insensitive file systems) and the overlay accepted `.GIT/config`;
  `git diff` rewrote `.git/index`; a Windows-invalid name in history crashed every commit copy; a
  failed baseline left an open session with no attempt; a 17.5k-line changed lockfile cost 4.8 s per
  attempt (now diffed once per session, with difflib's junk heuristic above 2,000 lines: 0.13 s);
  `core.symlinks=false` symlinks were reported as added; a commit-copy run attached to a claim counted
  as support; `debug differential` exited 0 when inconclusive.

The first fix of the ranking (tiers before "already there at attempt 0" whenever the coarse signature
changed) cost two causes on debugloops_v1 (run 7: cause named 5/8); the rule that replaced it (code
before tests, comment-only files last, and "same symptom" also when the failing tests are the same)
restored 7/8 (run 8). Both runs are in-sample.

### 7.6 Review round 3 (2026-09-25) and what changed

A third review (20 findings, scripted sessions on orders_app / node / glow_mod copies) found false
resolutions, false stops and wrong strategy conclusions; each was reproduced and changed as follows
(regression tests in tests/test_debug.py, test_looprules.py, test_failsig.py, test_treestate.py,
test_experiments.py):

- **Closing as resolved** also needs a run of the repro in the session that failed (or timed out) before
  the passing attempt - never the baseline itself; `debug start` says when the baseline did not fail
  (`reproduced: false`, exit 3). An agent report closes only a repro Verinoda cannot run itself (else:
  confirm with a Verinoda run), and agent reports of one tree that disagree are flaky for close too.
  `--accept-test-edit` also covers earlier failing tests that the accepted test change removed or
  renamed (named in the close note); skipped and xfailed ones still block.
- **test_edited** holds whatever else the attempt changed: a changed literal (an expected value in a
  parametrize table, a golden file under `tests/`), a removed test file, `if ...: return` / JavaScript
  `return;` / `t.skip()` / `.only(` inside a test. With doctests (`--doctest-*`, or a failing doctest
  id `path::module.func`) a changed example line of a docstring is a test edit; the rest of that module
  is code (a fix there is not a test edit), and a docstring holding `>>>` examples counts as code in
  the code identity. In `pyproject.toml`, `setup.cfg` and `tox.ini` only pytest's own section (and
  tox's commands) selects tests; a packaging `exclude` does not.
- **failing_tests_skipped**: a module that failed to collect is satisfied when its tests ran; after an
  early stop (`-x`, `--maxfail`, `--stepwise`; the plugin records it) tests that were not reached are
  not skipped ones. A pytest collection error (exit 2) is a failing run of the repro in the ledger.
- **Test ids** lose the run's throw-away paths (tests parametrised over absolute paths kept a new id in
  every copy: "flaky", and every pass "skipped" the failing test). Collection errors and doctests get a
  complete signature (pytest's section text; the crash location when no in-repo frame exists).
- **Differential and bisect judge a run on the symptom's tests**, not its exit status: the base failing
  only other tests is "the cause is in the diff" (with them named), some of the symptom failing there
  is `partly_in_diff`, a different failure is inconclusive; bisect counts a commit where every test of
  the symptom passes as good, skips one that fails differently, and says when its first bad commit
  shows only part of the symptom.
- **Runs**: processes a command leaves running are stopped when it exits (Windows: a job object the
  child joins while suspended - a venv launcher starts the interpreter within milliseconds; POSIX: its
  session), the throw-away copy is removed with retries, and a leftover is named. A runner that exits 0
  having run no test (node `tests 0`, `No tests found`, `Ran 0 tests`, cargo/go) is inconclusive.
- **Ledger**: output-only pytest options (`-v`, `-q`, `--tb`, `-r`, `--color`, `-s`, `-p
  no:cacheprovider`, ...) do not make another command; attempt numbers are taken at the insert (two
  ledger commands at once no longer lose one); printed commands are quoted; strategy costs include the
  measured overhead (the copy), and a commit's content ids are read in batches.

Not changed: per-test outcomes (so `failing_tests_skipped` and the symptom judgement by test) exist for
pytest only; other runners fall back to the coarse signature. A per-session reusable copy (7.4) is
still not built.

## 8. Change review (D35, 2026-09-25)

### 8.1 Findings that drive the design

- `verinoda map --view impact` seeds whole files, climbs `method` and `imports` edges and cuts its lists
  silently (80 symbols, 40 files): on a one-function change of the 380-file Verinoda copy it listed 80+
  symbols in 40 files; on forge_mod a tick handler reached 21 items through its class while the real caller,
  a `Cls::method` ticker registration, is no edge at all. It says nothing about which definition changed or
  what the change touches (a value written to the database, a removed check, a loop in a tick).
- Nothing classified a change by concern, nothing linked a diff to the tests that reach it, and the skill had
  no review step after an edit.

### 8.2 Decisions

- **Surfaces**: `verinoda review [PATH] [--base REF | --staged] [--target FILE[::Qual] ... --change
  body|signature|remove] [--concerns ...] [--run-tests] [--observe] [--max-chars N] [--json]` and MCP
  `change_review` (same arguments). Exit 0 = no finding at strong_inference or above and no unknown; 3 =
  findings or unknowns to report; 1 error; 2 usage. The review is stored in `analyses` (`rev_...`,
  `question = "review <base>..<mode> <hash>"`): no schema change.
- **The diff**: the working tree (tracked files, and untracked ones only when they are source, config or
  documents; binaries skipped and listed) or the index against a base resolved with `rev-parse --verify
  --end-of-options` (a ref starting with `-` is refused), read through `treestate` (plumbing only). Planned
  targets must be repository-relative paths inside the project. **Deviation**: a stale git index (a copied
  checkout) makes git report every file as changed and the review compares each one by content - correct but
  slow (11 s instead of 2 s on the 380-file copy); the review never refreshes the index (that would write
  `.git/index`).
- **Changed definitions**: `anchors.compute_facts` of both versions, compared per definition: `signature`,
  `body`, `added`, `removed`; module statements (`module_statement`, named by the names they bind), changed
  keys of config files (`config_key`, by line and key path), `file_only` without facts. A definition whose own
  lines did not change (only a nested one) is not reported. **Deviation** found on the dev fixtures: for
  grammars without a `body` field (Kotlin) the facts' signature hash covers the whole definition, so the text
  before the body decides signature vs body.
- **Dependents by change kind** over the last snapshot's graph, depth 3, never through `method` edges: body -
  calls, references and construction (callers of the class run `__init__`); signature - also imports,
  inheritance and uses; removal - every reference (depth 1). Each dependent carries its via-chain (relation,
  line, EXTRACTED / INFERRED; lines in changed files re-found by name) and, for Python, whether it receives the
  changed value (def-use per hop). The list is capped at 40 with the total and `dependents_truncated`; readers
  of changed module-level bindings (Python names; `Class.CONSTANT` of a JVM class whose static initialiser
  changed) are listed separately. The graph is not rebuilt: Python calls in changed functions are re-resolved
  from the working tree's syntax tree; for other languages the graph's call edges are used and calls on changed
  lines are matched to same-file definitions by name.
- **Concern rules** (tables in `review_rules.py`, each hit with `derived_by`):
  - persistence: a sink (`SINK_PATTERNS`, plus NBT saved-data rows) on a changed line; a changed call whose
    callee reaches a write (SQL, ORM, file, key-value, saved data) within 2 hops, one finding per changed
    function and kind; the changed function's value carried by its callers (Python def-use per hop: assignments
    and transforming builtins carry it, containers, object fields and other calls do not) into a call whose
    parameter reaches the callee's write statement. **Deviation** found on the dev fixtures (the
    Verinoda copy): a value reaching a callee that only *calls* a sink produced findings for any string that
    ended up in a record; the last hop must now reach the sink's own statement.
  - security: operations on changed lines (Python calls bound through imports and aliases by the decision
    guards' engine, `eval`/`exec`, `shell=True`, `verify=False`, unsafe `yaml.load`, SQL text built with
    f-strings / `%` / `+` / `.format`; text rows for Java, JS, Go); exit guards removed or changed by syntax-tree
    diff (Python `ast`, tree-sitter), where a guard is an `if` ending in raise / throw / break / continue or in a
    return of a fixed value (**deviation** found on dev fixture O01: a branch returning a computed value is
    logic, not a guard) - a guard whose condition moved to another changed or new function is `guard-moved`
    (weak_inference); permission checks written as calls (`hasPermission`, `requires`, `withLevel`, ...) removed
    or changed; a check function (`stillValid`, `is_*`, `validate*`...) made a constant return; security words
    on changed lines (weak_inference). For Python operations, the parameter flow from an entry point's
    parameter to the operation (def-use per hop, up to 3 callers) is attached.
  - performance: a call in a loop whose callee reaches a sink (N+1), or a sink in the loop body, on changed
    lines; with the loop bound when a `len(x) > N` guard is found in the function or in a check it calls before
    the loop, else the unknown `loop_bound`; a loop added, or a loop condition changed (a removed counter bound
    named), in a hot path: a method registered by `Owner::name` in a tick / render / chunk-load registration,
    a tick-like override name, a NeoForge tick event handler, or a Python request handler.
  - public_api: Python call sites bound to the changed function (imports, module attributes, `self`,
    annotated receivers, the graph's edges) whose arguments no longer fit the new signature
    (`statically_verified` when bound through an import or the module, a decorated definition at most
    strong_inference); Java/Kotlin call sites (graph edges, or `Owner.name(` in files that import the owner's
    package) whose argument count no longer fits, single-overload classes only; names removed but still
    imported or used (a name the module still binds - an import rewritten - is not removed).
  - config: environment reads on changed lines; names read on changed lines that are bound to an environment
    read or live in a config module; JVM config values (`*Config.X`) and quoted keys in config calls or present
    in the repository's config files; changed keys of config files with their readers (literal key search).
  - entry_points: the changed symbol or a dependent (depth 3) that is an entry by the map's heuristics or by a
    registration (client->server packets, commands, interaction callbacks, input overrides by name); strong
    with a decorator, a registration or two reasons, else weak_inference; functions nested in functions are
    never entries.
  - config files: a changed config file (by name: `.env`, `*.toml`, `*.yml`, `*.ini`, `*.cfg`, `config.*`,
    `.properties`) gives one change per changed key, an added or removed one a single change; other data files
    (JSON that is not configuration, game data, text) are listed under `files`, not reviewed by concern.
- **Status**: mechanical facts re-read from the current files are `statically_verified` (a call bound through
  imports on a changed line, a guard in the base syntax tree and not in the working tree's, an arity mismatch
  bound through an import); everything else is at most `strong_inference`, word heuristics `weak_inference`.
  These are the review's grades in the claims vocabulary; the findings are not stored as claims
  (**deviation**, 8.4). An empty concern says "no finding from rules: ..." with the rules that ran.
- **Tests**: static reach (the tracer's selection rule; tests as `verinoda/testcode.py` recognises them:
  JUnit/GameTest methods by annotation, vitest/jest `it()` calls, and steps the graph does not hold - calls
  through a dotted module path such as `pkg.main.run()`, what the pytest fixtures a test requests call, the
  names an `it()` body uses from its imports - each with a basis that names what the test reaches beyond
  distance 1), the tracer's
  latest complete run (tests that reached each changed Python function, labelled run-scoped and with its
  commit), `--observe` (runs the selected pytest tests under the tracer: reached, and selected-but-not-reaching
  in a complete trace) and `--run-tests` (the selected pytest tests through `experiments.run`); symbols no test
  reaches; `runtime_tests` unknown for Java/Kotlin (Gradle is not allowlisted).
- **Unknowns** with a next step: `runtime_tests`, `loop_bound`, `method_reference` (callers only by text),
  `no_callers`, `dynamic_callers`, `string_reference`, `graph_stale` (cited files changed since the snapshot),
  `unsupported_file`, `target_not_found`.
- **read_first**: changed spans (a long definition: the changed lines with context), first-hop call sites (+-3
  lines), finding and evidence lines (+-1) by status, merged per file, packed into `--max-chars` (default
  6,000); the rest goes to `budget.more` with its count.

### 8.3 Measurements (docs/BENCHMARKS.md, Update 2026-09-25: change review)

- review fixtures (`benchmarks/review_fixtures/`): 36 dev + 11 held-out changes on git copies of orders_app,
  glow_mod, forge_mod and a clone of the 380-file Verinoda copy, gold written and hashed before any rule
  (one amendment, before the first run, recorded in `MANIFEST.json`). The builder wrote the fixtures, the gold
  and the rules: the dev numbers are in-sample (the rules were written knowing them), the held-out set only
  guards against tuning on its answers.
- Dev, rules as frozen (commit 3b73872): precision 66/72 = 0.92 and recall 62/62 at strong_inference or above;
  every false positive is an entry point that does reach the change on a fixture whose gold listed no entry
  points (G03, G05, G06). Changed symbols exact 36/36, must-say-unknown 9/9, static test reach 22/22, gold
  dependents 19/19, gold locations inside `read_first` 62/62, silent truncation 0.
- Held-out, its only run with the frozen rules: precision 23/29 = 0.79 (bar 0.8 not met), recall 18/18,
  must-say-unknown 2/2, changed symbols exact 10/11 (HO4: two import statements the gold did not list). False
  positives: 4 value flows of `snapshot.git`'s output into records and 1 entry point on HV1 (the gold says no
  persistence), 1 import rewritten read as a removed name on HO4 - a bug, fixed after that run.
- After the frozen run: the HO4 fix and six fixes found by reviewing Verinoda's own branch against main with the
  tool (JSON data files read as config key by key, persistence through a callee counting reads, ungrouped
  findings, a guard moved into a new helper reported as removed, nested functions as entry points, "signature"
  as a security word). Held-out 22/27 = 0.81 (no longer clean for these rules), dev 65/71 = 0.92, recall
  unchanged; the branch review (321 changed definitions) took 48 s, 85 s before the fixes.
- Time per review with the graph loaded (the CLI's case, index refreshed as `git status` does): orders_app
  0.19 s median, forge_mod 0.21 s, glow_mod 0.20 s, the 380-file copy 1.8 s median and 5.4 s max (bars 2 s /
  6 s); a cold CLI process (Python start-up included) 0.66-0.95 s on the examples and 2.2-2.4 s on the copy.
  With the graph kept in memory (MCP): 0.13-0.16 s and 0.8 s median.
- Blast radius against `map --view impact` on the same diff: 67-100% fewer items on every dev fixture with
  dependents (A1 = O01: 3 vs 11; A7 = F02: 0 vs 28, its caller being a registration reported as an unknown;
  A8 = V01: 7 vs 80+, the view's list capped), gold dependents 19/19.
- Gold locations inside `read_first`: 62/62 dev and 18/18 held-out, median 642 characters per dev fixture; the
  baseline (the impact view plus the changed and affected files) covers the same files at a median of 11,530
  characters and exceeds 6,000 on half of the dev fixtures.
- A8 with `--run-tests`: 19 selected tests, `test_policy_rejects_arguments_that_leave_the_copy` among them; the
  run failed (8 failed, 66 passed); the failures read in the log are the `..` cases the removed guard handled.

### 8.4 Not done / limits

- Findings are not stored as claims and critique does not run on them; no entail predicate grades a carried
  value (the review's own def-use check does, Python only).
- Which changed lines the tests execute (a LINE-event tracer option) is not built; `--observe` reports which
  tests reach the changed functions, and `--run-tests` runs the selected tests without the tracer (pass/fail).
- Nested loops over one collection and unbounded appends to module-level containers are not checked; value and
  parameter flow exist for Python only; Java callers through method references are found by text (inference).
- Entry and hot-path tables cover web decorators and handler names, NeoForge and Fabric registrations and
  tick-like override names; other frameworks give silent misses (a concern without findings names the rules
  that ran in `concerns_checked`).
- The graph is the last snapshot's: a new caller in an unchanged file appears only after `verinoda update`.
- No agent session with and without the review step has been measured.

### 8.5 First review round (2026-09-25)

Two reviewers ran the review on changes of their own - scripted repros on small projects, and 47 changes one of
them labelled on the example copies and the Verinoda clone before running it - and reported 41 findings (11
high, 20 medium, 10 low). Each was reproduced on commit 8e2cc3b and fixed with a regression test (39 tests in
`tests/test_review.py`, section "review round 1", each failing on 8e2cc3b); the time finding was partly fixed.
What changed in the decisions of 8.2:

- **The staged tree is the index, everywhere.** `git diff-index --cached --raw` names both blobs of each staged
  path (the old `ls-files -s -- <paths>` put every path on one command line: 900 staged files overflowed it on
  Windows and every file read as deleted; a git failure is now an error). Files outside the diff whose working
  copy differs from the index (`diff-files`) are read from the index, the file list is the index's, and the
  guards engine reads the reviewed texts - before, an unstaged test update hid a staged arity break and an
  unstaged `subprocess.run` gave a verified finding. `--run-tests` runs a copy of the base commit with the staged
  files laid on top (each must hold its staged content in the working tree) or refuses; `--observe` refuses
  unless no tracked file differs from the index.
- **Text as Python reads it**: a leading byte-order mark is dropped (such files did not parse and got no rules).
- **Graph nodes and edges**: a definition takes a node only when the node's line or class fits it (a new function
  `save` took the node of the method `OrderRepository.save`); incoming edges skip documentation, and INFERRED
  edges are dropped when they cross into another project root of the repository (its own pyproject.toml,
  package.json, build.gradle ...) or come from a Python file that imports a namesake module from elsewhere (a
  vendored copy's `store.save()` bound to Verinoda's `Store`: 43 foreign tests, 4 foreign entry points and a
  `--run-tests` run that could not collect).
- **Python binding**: module attributes through `from pkg import m`, `import pkg.m` and `import pkg.m as x`; a
  name bound in the calling function (a parameter named like the changed function) is no call to it; a removed
  method is still used where `self.` / `cls.`, the class name, an annotated or constructed receiver or a graph call
  edge still calls it (strong_inference; weak_inference when base classes may provide it), and when nothing binds,
  `.m(` calls through unresolved receivers are an unknown. JVM removals: `Owner.name(`, `Owner::name` and calls in
  the own class are strong_inference, `x.name(` on any other receiver weak_inference (a removed `Cart.get` matched
  `prices.get(name)` on a Map).
- **Public API**: a planned signature change lists the call sites (weak_inference: there is no new signature to
  check); positional parameters whose names changed places give `positional-order-changed` at each call site.
- **Persistence**: a value bound on a changed line and written later in the same function - by a sink statement or
  through a callee's parameter into its sink statement - is `changed-value-to-sink` (the rules only covered sinks
  on changed lines and the changed function's return value); removed sink lines are found by comparing the sink
  lines' code as a multiset (a removed `commit()` hid behind the INSERT that stayed); `setChanged()`, `INSERT OR
  REPLACE` / `REPLACE INTO` and JVM `Files.*` / file-stream rows join the tables (file reads count for performance
  only); an NBT key read that the class never writes, or written and never read, is `saved-data-key-mismatch`
  (it was a "config key"); a class reached through a constructor call holds only its own lines as sinks.
- **SQL text** counts only when it is SQL-shaped: upper-case keywords, a column list, a WHERE / VALUES / SET / JOIN
  clause or a placeholder ("Select at least one item from the catalogue" is not); SQL built from strings is
  statically_verified only when it reaches an `execute`-like call in its function, else strong_inference.
- **Security operations**: calls bound through the file's imports (`from yaml import load`); an operation the base
  version had on its changed lines too "holds ... too" at weak_inference (strong when an entry parameter reaches it)
  instead of "adds" at statically_verified.
- **Guards**: conditions are compared with parameters renamed by position (a renamed parameter changed no guard).
  Before `guard-removed`, the review rules out a move to another changed definition, a split (`a or b` into two
  guards: `guard-split`, weak), an extraction (a new guard calling a helper whose single return expression, with
  the call's arguments put in, is the old condition: `guard-moved`, weak), a change (the closest new guard, or a
  wider exit guard holding the condition: `guard-changed`, verified), and a restructuring (the negated condition
  inside another condition: `guard-restructured`, weak). The same condition still tested by an if that no longer
  exits is `guard-removed` and says so. A call removed from a changed function to a check - a callee that raises on
  bad input or is named like a check - is `check-call-removed` (Python, strong_inference).
- **Constructs, not lines**: rules "on a changed line" report a construct only when the base version's changed
  lines do not hold it too: calls (text with parameters renamed), environment reads (the whole call, so a changed
  default still counts), config names (the operand around them), JVM config keys (the call they are an argument of)
  and config reads (the operand). A rename no longer reports the unchanged `get_repo()` beside it.
- **Performance**: a for-loop's body excludes its iterable; a literal iterable states its size and caps the finding
  at weak_inference; IO in a loop that the same base loop already had is weak_inference ("was there before"); a
  hot path by registration or tick event is strong_inference, by method name (JVM files that import a game
  framework only - `update` in TypeScript is no game tick) or by request-handler heuristics weak_inference, and a
  loop added there without IO is as strong as that evidence; JVM calls `Owner.name(` on changed lines resolve to a
  class of that name the file can see.
- **Entry points and parameter flow**: a Python definition without a graph node gets the map's entry heuristics
  from its syntax tree, and parameter flow follows callers found in the changed files; changed keys of registration
  manifests (`fabric.mod.json`, `mods.toml`, `plugin.yml`, mixin configs, `package.json` main/bin/exports/scripts)
  are `registration-manifest-changed` (JSON key paths no longer carry the first key as a prefix).
- **Config**: findings are one per place, rule and key (a renamed key is two findings); a removed config-file key
  names the code still reading it; a field of a config class (`*Config`, `*Settings`, `*Defaults`, `*Options`)
  initialised on a changed line is `config-default-changed` with its readers; a verb member such as
  `GlowConfig.load()` is no config value.
- **Tests**: static reach also runs from the function a nested function is defined in, from the callers in the
  changed files of a definition the snapshot does not have (added, renamed), and from the tests of changed test
  files that call a changed symbol. `no_test_reaches` lists only symbols with a static caller; the others are in
  `tests.reach_unknown` - tests may reach them through a callback, a registration or a subprocess run, as
  `tests/test_cli.py` reaches `cmd_map`. The run reports its source (working tree or staged tree), the tests
  selected and how many the cap of 50 left out (nearest first).
- **Output**: duplicate findings (a class and its method covering one line) are one, for the innermost symbol; a
  rename (an added definition whose code is a removed one's with the name replaced) is marked `renamed_from` and
  `read_first` shows its new header only; the text output names changed data and documentation files.
- **CLI**: `--target` with `--base` / `--staged` and `--max-chars` below 1 are usage errors (exit 2); PATH defaults
  to the project found from the working directory.
- **Cost**: facts of the reviewed texts go through the content-addressed facts cache, code text is cached per
  content, a tree's definitions are indexed once, def-use results are kept per function, the guards engine is
  skipped when no target name is on the changed lines, and the Python call-site, removed-name and reader searches
  read the files that import the module in the graph (plus the changed ones) instead of every file.

Measurements (docs/BENCHMARKS.md, "change review, first review round"; fixtures and gold unchanged, fresh base
copies): dev (in-sample) precision 67/73 = 0.92, recall 62/62, must-say-unknown 9/9, changed symbols 36/36,
static test reach 22/22, gold dependents 19/19, gold lines in `read_first` 62/62; held-out (no longer clean)
22/27 = 0.81, 18/18, 2/2, 10/11, 3/3, 18/18. The false positives are those the builder's fixes left. The dev gold's
"no test reaches" items went from 4/4 to 1/4 on purpose: the other three have no static caller and are now
`reach_unknown` (the gold was not changed). Time with the graph loaded, other agents running: 0.19-0.24 s median on
the examples, 1.71 s median and 1.87 s max on the copy's dev fixtures (V03 was 5.0 s), 3.7 / 6.0 s on its two
held-out fixtures.

Still not done, beyond 8.4: a test run whose collection fails is not retried without the failing files; value
flow into later statements, removed check calls, moved positional parameters and entry heuristics for new
definitions are Python only; `map_view.go` (reviewer 2's R35) stays `reach_unknown`, since the graph has no edge
into the MCP tool that encloses it; a 40-definition diff on the copy takes 8.7 s (the design's 6 s bar was set
for fixture-sized changes; 13.4 s in the reviewer's run).

### 8.6 Second review round (2026-09-26)

A second reviewer ran scripted repros against commit 42d9b41 (and against 8e2cc3b and ed1c891 to tell
regressions of the first round's fixes apart) and reported 20 findings (7 high, 8 medium, 5 low; 11 of them
regressions of 8.5's relaxations or cost cuts). Each was reproduced on 42d9b41 and fixed with a regression test
in `tests/test_review.py` (section "review round 2"; 17 new tests and 2 extended ones, all 19 failing on 42d9b41).
What changed in the decisions of 8.2 and 8.5:

- **A check counts as the same check only while it still runs before the work.** A guard kept, split, extracted
  into a helper or moved into a callee that now runs after a sink statement or a call reaching one (within 2
  hops) that the base guard preceded is `guard-after-work` (statically_verified when the statement holds the
  sink itself, strong_inference through a callee); a call to a check (a callee that raises on bad input or is
  named like a check) moved below such work is `check-call-after-work` (strong_inference). Statements are
  compared as text (Python calls by their source), and a text the base version already ran before the guard as
  often is not counted. Before, a split guard whose second half followed the DELETE, or a guard calling an
  extracted helper after the commit, was "the same check" (weak_inference, exit 0).
- **guard-moved** needs a call between the two definitions (the guarded one calls the destination, or the
  destination calls it - then the check must precede that call); the same check text added to an unrelated
  function is no move, and the removal stays `guard-removed`.
- **guard-restructured** needs a condition the base version did not have (the base's conditions, the removed
  guard's own aside, are consumed first) whose branch holds a statement that followed the guard in the base
  version. An unchanged `if is_admin(user): audit(...)` after the write no longer turns a deleted
  `if not is_admin(user): raise` into a weak finding.
- **Security operations on changed lines** are compared as calls, not as operation kinds: "holds ... too"
  (weak_inference) only for the same call text with parameters renamed by position; the same kind of call with
  other arguments "changes" it - statically_verified when its arguments read a name they did not read (a
  constant command became a parameter), else strong_inference (a keyword with a constant added). Text rows of
  other languages compare the whole code line.
- **The guards engine is not skipped** for calls through a name that binds a security target under a name of its
  own: an assignment (`run_cmd = subprocess.run`) or a project module's renamed re-export (`from app.compat import
  run_command`, followed up to 4 modules deep); those names are passed to the engine, which found neither before
  (the re-export was missed in every version).
- **Files newer than the snapshot**: code files the last snapshot does not have with their current content are
  searched by the Python call-site, removed-name and reader rules, and those naming a changed definition are a
  `graph_stale` unknown (their edges are missing from the dependents). 8.5's cut to the graph's importing files
  had silently dropped a caller committed after the last `update`. **Deviation** (cost): a file the snapshot has
  and that was last written more than an hour before the snapshot was recorded is taken to be unchanged, not read
  and hashed again; a tool that backdates modification times, or a scan that took longer than that, would hide a
  newer caller there.
- **Projects of one repository**: an INFERRED edge into another project root is kept when the caller imports
  from that project (Python: its top-level package under the callee's project root; Java / Kotlin: an import of
  the class or the same package; JS / TS: a relative import into it or its package.json name). Module names under
  a project root count for the namesake rule too (`libs/core/core/repo.py` is `core.repo`). Before, every
  cross-package edge of a monorepo or of a multi-loader Gradle mod (`common/`, `fabric/`) was dropped.
- **`--staged --observe`** refuses when a staged file has unstaged changes (or a staged deletion is still on
  disk), as `--run-tests` did; it traced the working tree and called it "equal to the index".
- **IO in loops**: every call of the loop that reaches IO is compared with the base loop; a new one is reported
  (strong_inference, the calls that were there named), not the first one found.
- **Removed Python methods**: base classes are resolved through the file's imports; project bases (and theirs)
  without the method keep callers at strong_inference, a base outside the project or unresolved caps them at
  weak_inference; `.m(` calls that no rule bound are an `unresolved_callers` unknown also when other callers
  were bound.
- **Removed sinks** are compared over the definition's own spans only: the file's line diff had paired a
  removed `commit()` with the same line in a new method below it. A removed sink line whose code is now in a
  helper the definition newly calls (`self._flush()`) is a move at weak_inference (found when re-running the
  reviewer's repros: without the diff filter it had become a strong "removed").
- **Call sites**: `Cls(...)` is a call site of `Cls.__init__` (arity checked, statically_verified through an
  import); a name imported through a package's re-export (`pkg/__init__.py: from .rules import validate`) binds
  to the changed function for call sites, removed names and readers.
- **JS / TS removals** of a top-level function: strong_inference only for an import of it from its module and
  calls through that import (or a namespace import); the word elsewhere - a template string, an import path, an
  object key, a field - is weak_inference (at most 5).
- Lows: package.json scripts other than lifecycle ones (`start`, `install`, `prepare` ...) are weak_inference
  entry-point changes; readers of `.env` keys include `process.env.KEY` / `import.meta.env.KEY`; `--concerns` with
  an unknown name is a usage error (exit 2); a config record's default lists the readers of the changed component's
  accessor (`.maxDistance()`) in the files that see the class; an import statement edited in place is one
  `module_statement` change with `base_names`, whose dropped names are still checked as removed.

The held-out run after these fixes showed one duplicate they had introduced (HV1: a changed `subprocess.run(...)`
and the `shell=True` added to it were two findings); a weaker finding on the same call is now folded into the
stronger one. Measurements (docs/BENCHMARKS.md, "change review, second review round"; fixtures and gold unchanged,
base copies indexed in the same session): dev (in-sample) 67/73 = 0.92, recall 62/62, must-say-unknown 9/9,
changed symbols 36/36, gold dependents 19/19, gold lines in `read_first` 62/62; held-out (not clean) 22/27 = 0.81,
18/18, 2/2, 10/11, 3/3, 18/18 - the first round's numbers: the fixtures hold none of the reviewer's cases. Time,
interleaved A/B on the same base copies with the graph loaded: +9 to +13 % on the copy's dev fixtures (1.19-1.25 s
-> 1.32-1.41 s), within noise to +10 % on the examples; most of it is the check of files newer than the snapshot.

Not done in this round: the reviewer's labelled change set (R01-R47 of round 1) was not re-run as a whole; JS / TS
and JVM removals still bind by text (imports and calls, not a resolver); a guard moved into a callee counts as
before the work when the call to that callee is.

## 9. Probing changed functions (D36, 2026-09-25)

### 9.1 Findings that drive the design

- Code that looks right and passes its tests can still be wrong on inputs the tests do not use: in
  `orders_app`, `>` turned into `>=` together with `round` turned into `int` truncation passes all five tests,
  and so does saving one row per item and returning the last id. The runner could only say what the existing
  tests already check.
- Nothing produced inputs: `analyze`'s behaviour handler checks static propositions only, runtime observation
  needs tests, hypothesis was a dev dependency only. The design's prototype (plain subprocesses) found the
  boundary change, the truncation and a new `OverflowError` in 315 inputs, and 0 differences for an
  equivalent rewrite.

### 9.2 Decisions

- **D36. `verinoda probe`** (`verinoda/probe.py`, CLI `probe FILE::NAME` or `probe --changed`, MCP
  `change_probe`): call one changed Python function on generated inputs at the base commit and in the working
  tree, through the experiment runner, and report what behaves differently. Deviations from the design are
  marked *(deviation)*.
- **Eligibility**: Python only (anything else is `unsupported` with the language named: "Kotlin: the probe runs
  Python functions only; use review and the project's own tests"); top-level functions, static and class
  methods, and instance methods whose class has a *recipe* - a constructor call with literal arguments in the
  code or tests (`Money(5)`), built fresh for every call inside the run. Coroutines, properties, classes and
  parameters whose type cannot be built are `unsupported`, with the reason and the next step.
- **Side-effect gate** (`probe_gate.py`), before anything runs: the target's static closure (4 levels through
  imports, re-exports, `self.method()`, constructors and parameters annotated with a project class; names
  resolved with the guard engine's `_PyIndex`), the recipes' constructors, and the module-level statements of
  every project module they live in or import. It refuses file writes, network, processes and threads,
  database connections, `architecture_map.SINK_PATTERNS` lines (reads allowed), and writes to state the probe
  cannot isolate between calls (a `global` it assigns, a module-level container it mutates, an imported
  module's or a class's attribute, the environment, `sys.path`, the random seed, handlers). An SQL write
  statement counts where it reaches a call's arguments (written there, or through a local, module or imported
  name assigned one, a parameter default, an instance attribute set in a method, another project class's or
  module's constant, a loop variable over statements, or a project function that returns one: `execute(_sql())`),
  not where it is only returned or assigned, nor in calls that only build or log text (`.format`, `.join`,
  `log.info`, an exception); `.commit()` / `.save()` on `self`, a parameter annotated with a project class or a
  local built by a project constructor is followed into that class's method *(fixer: both were false refusals
  of pure functions; fixer 2: the first syntax-tree rule let six of those shapes pass)*. A constructor call also
  reads the class's `__del__`; `setattr(Cls, ...)` and a class-level container changed through `self`
  (`items: list = []` then `self.items.append(x)`; not a dataclass field) are class state *(fixer 2)*. The other
  text patterns are marked `heuristic`, and a refusal that
  rests only on them says "may reach ... (a text pattern the gate cannot confirm: `session.commit()`)". Every
  reason names the line and the call chain: `refused: place_order reaches sql-write at orders/repository.py:17
  (place_order -> OrderRepository.save)`. Reading the environment at import is not a side effect.
  `--allow-side-effects` is the user's decision; the reasons stay in the result.
- **Audit hook** *(deviation: the design attached the call tracer to list observed boundary calls)*: the
  plugin installs `sys.addaudithook` and, while the target module is imported and while each call runs - and
  for the whole process in every thread the project starts, so a thread that outlives its call stays
  restricted - blocks file writes, network, process starts (`subprocess`, the `os` process functions, and
  `multiprocessing` children through `_winapi.CreateProcess` or `_posixsubprocess.fork_exec`, which the plugin
  makes raise an event), environment and working-directory changes and database connections other than
  `:memory:` by raising a `BaseException` subclass inside the operation (a function that swallows it is still
  recorded as blocked). From the import on, the main thread is restricted between and after the calls too
  *(fixer 2: a result's `__del__`, a `weakref.finalize` callback and an `atexit` handler ran after the call's
  window had closed and wrote for real)*: what a call made is released, and young cycles collected, before its
  window closes, so its finalizers count as the call's; `atexit.register` after the import keeps the handler in
  the plugin, which runs it under the hook after the calls; the process then ends with `os._exit`, so nothing
  left for interpreter exit runs unobserved (an object still alive then is not finalized at all). At import and
  exit, writes inside the run's throw-away directory (library caches under the run's HOME) are allowed; the
  loopback pair `socket.socketpair()` builds on Windows (`asyncio.run`) is not network *(fixer 2: it was
  refused)*. Any blocked call, or a blocked event outside the calls (a project thread, a finalizer, an exit
  handler), makes the probe `refused` ("at run time"), with the event and where it ran, and no difference is
  reported; threads still running after the calls are waited for (at most 5 s), then the run ends with them and
  a limit says so. With `--allow-side-effects` the events are only recorded. Not seen at run time *(fixer: the
  first version claimed class-attribute writes; CPython raises no audit event for them)*: class-attribute writes
  (the static gate checks those) and what C extensions or `ctypes` do without an audited Python call.
- **The module called is the file named** *(fixer)*: after the import the plugin compares the module's
  `__file__` with the target file in the copy; a module whose import name another module took first (a
  namespace directory holding `json.py`, `copy.py` ...) is `inconclusive` ("`json` resolves to
  .../Lib/json/__init__.py, not tools/json.py") and nothing is called.
- **Inputs** (`probe_inputs.py`, syntax trees only; the project is never imported in Verinoda's process):
  annotations (scalars, `Decimal`, Optional/Union, containers and their `typing` spellings, `Literal`, enum
  members read from the class body, project classes through recipes), refined by call-site literals (followed
  two callers up: `place_order(repo, c, items)` passes `items` on to `validate_items`); boundaries mined from
  both versions of the function and from the project functions it calls - `x OP C` gives C, the next float
  either side, C +- 1 (+0.05, +0.005 for floats), `len(p) OP C` and slice bounds give sizes C-1, C, C+1, `min`/
  `max` arguments, compared strings, rounding calls add values on rounding edges - with C resolved through
  module constants, imports and `os.environ.get("K", "50")` defaults (the defining line is kept: "boundary
  100.0 from orders/pricing.py:13 (DISCOUNT_THRESHOLD, set at orders/config.py:7)"); standard edges (0, +-1,
  2^31/2^63 bounds, -0.0, nan, +-inf, 1e308, the smallest subnormal, '', whitespace, Turkish letters, ß, a
  combining mark, right-to-left text, emoji, 100,000 characters, empty and 10,000-element collections, None
  where Optional); defaults are exercised by leaving them out. Numeric fields of a dict shape vary one at a time
  with the others at their unit value (qty 1), so `price * qty` reaches a mined boundary. Then generated values
  fill the budget (default 300): hypothesis when installed (`@seed`, generate phase, no example database; the
  new optional extra `verinoda[probe]`), else a fixed pseudo-random list from the same seed. hypothesis keeps its
  caches under the system temp dir for the call (nothing is written in the working directory) and does not mix
  in constants of the modules imported in Verinoda's process, so one seed gives one corpus in every process
  (both were found on the way: a `.hypothesis/` folder in the current directory, and corpora that changed with
  what else was imported). The corpus is one JSON file with its sha256, identical for both runs.
- **Runs**: `python -m pytest -q -p verinoda_probe -p no:cacheprovider --noconftest -o addopts=` through
  `experiments.run` (the allowlist, throw-away copy, scrubbed environment, tree-killing timeout and guarantees
  of every experiment; no new allowlist entry): the base as a commit copy (`ref`), the working tree as the
  default copy. The plugin and a generated `verinoda_probe_spec.py` are delivered like the call tracer; no
  project test is collected, and project `conftest.py` files are not loaded. Each input is called twice on
  freshly decoded arguments. A call past the per-call timeout (default 2 s) ends the process through
  `faulthandler`; the next run resumes after it (at most 3 times). A process that ends during a call without
  faulthandler's stack in `probe_hang.txt` (`os._exit`, a crash) is an exit with the run's exit code, not a hang
  *(fixer)*; a process that ends before the target was imported, or a failure of the plugin itself, gives no
  input at all and makes the probe `inconclusive` *(fixer: both used to become invented "hanging" inputs)*. A run
  that reaches its own timeout is not a hang: the inputs after it are "not run".
- **Oracles**: difference classes `new_exception`, `exception_type_changed`, `value_changed_at_mined_boundary`,
  `value_changed`, `type_changed`, `exception_removed`, `process_exit_changed` (the call ended the process on
  one side only, or with another exit code), `argument_mutation_changed` (a digest of the mutable arguments after
  the call) and `numeric_drift` (floats within 1e-9 relative; integers, strings, `Decimal`, `Fraction` and quoted
  text inside containers never drift *(fixer)*; only a float result or a built-in container of literals, and
  only where both numbers are float literals with different values - an integer that became a float, `0.0` /
  `-0.0` and an object's own repr are `value_changed` *(fixer 2)*), compared on the result's type and `repr`
  with memory addresses masked (integers past the 4300-digit `str` limit are rendered with the limit lifted for
  the rendering only), and on exception types (messages are not compared, and `not_checked` says so). A
  `value_changed` example
  whose two results are `==` equal (dict key order, `-0.0`/`0.0`) is marked `equal_under_eq`: still a change,
  the order is observable. An input that is both a call-site literal and a mined boundary keeps its boundary
  tag. Inputs whose two calls disagree are `nondeterministic`, never a difference.
  Undeclared exceptions: raised on inputs of the annotated domain and not declared by a `raise` in the closure,
  a `pytest.raises` around a call in the tests or the docstring (subclasses of a declared project exception
  count as declared); reported always, and the status without a base. Properties (`--property 'result <=
  subtotal'`) are evaluated per input inside the run with the arguments bound by name. `--scaling` times one
  list/tuple/set/string argument at 10^2, 10^3, 10^4 elements (median of up to 5 samples, 3 rounds; a size
  predicted or measured over the 1 s budget is not called again) on both sides, and flags `growth_changed` only
  when the working tree's growth per 10x is at least 3x the base's in every round.
- **Timeouts are not differences** *(deviation)*: without `--scaling` a call that ran past the per-call timeout
  on one side only is listed under `timeouts` ("slow or not ending, not judged") and makes the status
  `inconclusive` if nothing else differs; the design's "without --scaling nothing about performance" rule was
  read to cover it (a quadratic loop at 10,000 items took 1-2 s per call here and would otherwise be a
  machine-load-dependent "difference").
- **Confirmation and minimisation**: the three simplest inputs of every class (no special floats, ASCII,
  short; inputs derived from the code before generated ones; small numbers) run again in a fresh pair of runs;
  a class whose outputs do not reproduce is `unstable` and not reported as a difference. The design's second
  round of simplified neighbours is not built.
- **Recording** *(deviation: no `probes` table, no schema change)*: the runs are ordinary experiment rows, the
  corpus and the result are files under `.verinoda/runs/<probe id>/` (`corpus.json`, `probe.json`). Every
  confirmed class is a `behaviour` claim ("In probe P (base run R1 at commit C, working-tree run R2 at tree T),
  `apply_discount(100.0)` returns 100.0 at the base and returns 90.0 in the working tree
  (value_changed_at_mined_boundary).") with one evidence row of type `experiment` anchored on the function's
  definition line (it goes stale with the body) and `meta.kind = probe_counterexample`. `entail` grades it
  `full` only for the claim's own probe, input and class, reproduced in the confirmation runs, so the claim is
  `experiment_verified`; "no difference" is a `weak_inference` claim over the inputs listed
  (`probe_summary`, graded `partial` at most). `--emit-test` prints pytest functions that pin the base
  behaviour, with a comment that whether the change is intended is the user's decision; nothing is written to
  the repository. A result the plugin did not render as plain `repr` is rebuilt the same way in the test (set
  elements sorted by a helper written into the file, the int digit limit lifted around the check); one with
  masked addresses or run paths is a `pytest.skip` with the reason *(fixer 2: those tests failed on the base)*.
- **Honesty**: a difference is a behaviour change, never judged a bug. Statuses: `differences_found`,
  `numeric_drift_only` (only float drift: low priority, its own status and headline *(fixer: it read
  "differences_found ... behaviour changes")*; the drift is still an `experiment_verified` claim, a true
  observation), `property_violated`, `no_difference_found` ("in N inputs, a search, not a proof"; N counts the
  inputs that returned or raised on both sides), `nothing_found` (no base),
  `undeclared_exceptions` (no base), `refused`, `unsupported`, `inconclusive` (import error in the copy,
  failed runs, differences that did not reproduce, timeouts only, low input diversity, mostly nondeterministic,
  fewer than half of the inputs returned or raised - the rest hung, ended the process or were not run *(fixer 2:
  4 hanging inputs of 300 read "no behaviour difference found ... in 4 inputs", exit 0)*).
  None of the last three ever reads as a pass. Every result lists `not_checked` (side effects and state beyond
  the arguments, inputs outside the generated domain, exception messages, concurrency, iteration order that
  depends on string hashing - every run pins `PYTHONHASHSEED=0`, the same on both sides, which keeps
  `list(set(...))` from being a false difference but also hides that its order is not stable - performance unless
  `--scaling`), the run's `guarantees` and limits. The CLI exits 3 unless the status is `no_difference_found` /
  `nothing_found` (`--changed`: `done` / `nothing_changed`).
- **`--changed`**: the functions (top-level and methods of top-level classes) whose signature or body facet
  (`anchors`) differs from the base, or that are new; test files are left out; at most 10 probes. A function
  whose behaviour changed only through a changed callee or constant is probed when named (fixture D19). The
  listing's status is `differences_found` when any probe found something, `done` only when every changed
  function was compared and nothing was found, else `incomplete` (a refused, unsupported or inconclusive probe,
  or one over the limit: no pass, exit 3) *(fixer: it was `done`, exit 0, with every function refused)*. A
  changed file that no longer parses is listed as itself (`unparseable`) and makes the listing `incomplete`
  *(fixer 2: it was `nothing_changed`, exit 0)*.
- **Skills**: after editing Python functions, `verinoda probe --changed --json` (MCP `change_probe`) with
  `--property` for what the user asked; compare each difference with the request and ask when unclear; say "no
  difference found in N inputs", never "verified"; `--allow-side-effects` only after the user agreed.

### 9.3 Measurements (docs/BENCHMARKS.md, Update 2026-09-25: behaviour probe)

All in-sample: the fixtures, their gold, the mutant labels and the probe have one author. The gold of the 49
hand fixtures was fixed (sha256 recorded) before the probe ran on any of them; the design's B1 (fixture D03)
had been run once during development before.

- **Hand fixtures** (`benchmarks/results/probe-2026-09-25/fixtures.py`: 21 behaviour changes, 1 quadratic
  loop, 12 behaviour-preserving edits run with 5 seeds, 9 functions the gate must refuse, 4 it must let run, 2
  unsupported), first run and final code (c0dbd1f): detection 22/22 both (20/20 among the mutants the existing
  tests miss: D05 and D15 turned out to be caught by the fixture project's own tests, a fixture-writing
  mistake), gate 9/9 correct refusals (the gold sink kind named; R01 names `orders/repository.py:17`) and 0/4
  incorrect ones, unsupported 2/2, every reported class reproduced in its confirmation runs (37/37). The only
  reports on an "equivalent" edit are E04 on all 5 seeds - and the gold was wrong: turning `sum(generator)` into
  a loop changes results on Python 3.12, whose `sum()` of floats is compensated (`sum([0.1] * 10) == 1.0`, the
  loop gives `0.9999999999999999`); the probe reported it as `numeric_drift`, the low-priority class. On the
  other 11 equivalent edits: 0 differences in 55 runs. Without `--scaling` the quadratic loop gives no
  difference (0 performance claims); with it, `growth_changed` (about 100x vs 10x per 10x more items, every
  round).
- **Same fixtures with hypothesis blocked** (the fixed pseudo-random list): the same scores. **Without boundary
  mining** (ablation, 22 change fixtures, one seed): 19/22 - the boundary off-by-one at 100.0 (D01), the
  50-item limit through the imported constant (D04) and the 80-character limit (D11) are missed.
- **Automated mutants** (`mutants.py`: comparison flips, and/or, `not` removal, `+`/`-`, `*`/`/`, integer
  constants +-1, float constants +1%, over 12 functions of the fixture project): 45 mutants, 27 survive the
  tests; labelled by hand before the probe ran: 25 change behaviour, 2 change only an exception's message (the
  probe compares types: "no difference" on both), none equivalent. Kill rate 25/25. These are the same small
  functions as the hand fixtures: in-sample as well.
- **Time per probe** (300 inputs, both versions, confirmation runs, on a machine shared with other agents):
  median 2.1 s, p90 4.2 s, max 11.1 s (final code); the design's bar is 20 s. The slowest are the scaling run
  (10.6 s; 23 s before the fix that stops re-measuring a size already over budget) and `place_order` with side
  effects allowed (a 10,000-item order is 10,000 SQLite inserts per call). Refusals take about 0.1-0.2 s.
- **After the review fixes** (run 6, same fixtures and mutants): the same scores - detection 22/22, 0
  differences on the 11 genuinely equivalent edits, E04 now `numeric_drift_only`, gate 9/9 and 0/4, unsupported
  2/2, reproduced 37/37, kill rate 25/25; median 2.0 s, p90 3.8 s, max 11.2 s. The fixed shapes (threads,
  child processes, a taken module name, process exits, plugin errors, `Decimal`/`str` drift, SQL strings and
  project `.commit()` in the gate) are covered by regression tests, not by these fixtures.
- **After the second review round** (run 7, same fixtures and mutants): the same scores (22/22, 5/60 all E04,
  gate 9/9 and 0/4, 37/37 reproduced, 25/25 mutants); median 2.1 s, p90 3.3 s, max 10.9 s. The fixed shapes
  (finalizers and exit handlers, SQL through helpers/defaults/attributes/loops, int-to-float and `-0.0` drift,
  hangs-only passes, `asyncio.run`, an unparseable changed file, class-state writes the gate missed, emitted
  tests for sets and long integers) are covered by regression tests; the reviewer's own cases for these findings,
  rerun with its harness, now end as their gold says. Not fixed: an object kept alive to the end of the run is
  not finalized at all (no write, and none reported).
- Against the design's bars: B1-B4 and B8 detected 5/5 with minimal examples (`apply_discount(100.0)`: base
  100.0, working tree 90.0; `apply_discount(1e+308)`: OverflowError; `validate_items(<50 items>)`:
  ValidationError; `compute_total(<11 items>)`; `customer_key('a-b_c.d')` and the Turkish strings); kill rate
  25/25 (bar 60%); B5 and the other equivalent edits 0 false differences; B6 refused by default and a
  `value_changed` return id for 2+ items with `--allow-side-effects`; B7 unsupported (on a small Kotlin file,
  not the forge_mod copy); reproducibility 100%; time within the bar; `not_checked` in every result and the
  run's guarantees in every result that ran.

### 9.4 Not done / limits

- `verinoda review --probe` (the review is D35, another branch); the static concurrency signal (check-then-act
  on a global) the design placed in review.
- The design's second minimisation round (simplified neighbours), agent properties beyond one expression per
  flag, and a comparison of exception messages.
- Methods need a literal-argument constructor call somewhere in the project; objects built from other objects,
  fixtures or factories are `unsupported`. Parameters typed with a project class need the same.
- Only return values, raised exception types and the mutable arguments' state are compared: a function whose
  behaviour is a side effect (writing, sending) is refused, or - with `--allow-side-effects` - compared on its
  return value only.
- Process isolation only (container isolation exists in the runner and has never been run): the probe runs the
  project's code with the user's rights; the static gate is heuristic, and the audit hook sees what CPython
  audits (not a C extension's own writes, not a write through an already open file). An SQL statement that
  reaches a C-extension driver through a shape the gate does not follow (read from a file, or returned by a
  method of an object it cannot type: `self.repo.sql()`) is neither refused nor seen at run time. The
  project's exit handlers run right after the calls instead of at interpreter exit, and objects still alive
  when the run ends are never finalized (their side effects are neither made nor reported).
- `--changed` lists functions whose own body or signature changed; timing (`--scaling`) is rough and on a
  shared machine; the fixtures and mutants are in-sample (see 9.3).

## 10. Exact names and a fresh index (D37, 2026-09-26)

### 10.1 Findings that drive the design

A senior-engineer review (2026-09-26; five personas, gaps 2 and 6 of its synthesis) found:

- **Names resolved to the wrong thing.** On Verinoda's own repository, whose
  `benchmarks/corpora/heldout_repoatlas_7371990/` scan had just detected as a copy,
  `trace cmd_query search_index.rank` answered with hops inside the copy, `node_inspect rank`
  showed the copy's `evidence.py`, and `plan check` for "What calls assess_change?" asked the
  user to choose between the copy's function and `verinoda/claims.py`. `map --view impact`
  resolved targets with the fuzzy scorer: `asyncio/base_events.py::BaseEventLoop.call_soon`
  gave 0 affected, `...::NoSuchThing` gave 80, and nothing was reported unresolved.
- **A false edge between two packages.** The receiver-call pass took the first class of a name
  in the whole graph, so the copy's `cl.set_status` (`cl: Claims`, imported from the copy's own
  package) was linked to `verinoda/claims.py`.
- **Builds collided.** Two `update` runs a second apart: the second failed with `[WinError 2]`
  (the upstream rebuild lock is a no-op on Windows) and told the user to run `scan --force`, a
  three-minute rebuild. `analyze` during an update reported a refused shrink with the same hint.
- **Staleness was silent.** After an edit, `query` returned 1,500 tokens of an unrelated C# test
  for a function added to the edited file, `query --json` had no project-wide stale list, and
  `trace` answered "resolved by similarity to Widget" for that function. `analyze` right after a
  one-line edit spent 92-108 s refreshing inside its 60 s budget and answered unmet with 0 claims.

### 10.2 Decisions

**D37. One exact resolver; one build at a time; freshness on every read.**

- **One exact resolver** (`naming.resolve`) for trace endpoints, `map --view impact` targets and
  MCP `node_inspect`: the node the text names exactly (`retrieval._names_exactly`). Among several,
  a node in a detected copy (`copies.json`) or a configured reference tree gives way to the
  project's own code unless the text names that tree (`copies.roots_not_named`, the rule search
  already used); then a definition in test code or under an example, sample, fixture or vendor
  folder gives way to the product's own, then a function's local definition (a helper nested in
  another function, told by a `contains` edge or the enclosing function's parsed span) to a
  module- or class-level one; within one file a class is kept over its own constructor and a
  symbol over the file of that name. What is still tied is `ambiguous`: listed, none picked
  (trace used to pick the first and say so). A top-level function and a method of that name in
  other files are a tie: the first version preferred "no method in-edge", which made a test's
  nested `call_soon` win over asyncio's two methods and an Apex fixture's `update` statement win
  over three `update()` functions (review of the branch, section 10.5). What gave way is listed in
  `set_aside` and in the note, and trace and `map --view impact` print the node each name
  resolved to. `module.name` where that module imports `name` is the imported node (the
  same object, found through the import edge). A code name that names no node is never replaced by
  a similar one: `not_indexed` (spelled in a file changed since the index; those files are read
  first, so a large repository's time-capped scan cannot hide it), `not_found` (spelled nowhere) or
  `not_a_symbol` (spelled somewhere, a module constant or an attribute: where it occurs, with the
  nearest symbols as hints; round 2 had kept the similar node here, which answered about another
  name). Plain words may still resolve by similarity (`fuzzy`). Impact accepts an exact name only;
  any other target is `unresolved` with its candidates, and the CLI exits 2 for a `--target` that
  is. The question plan's mention linking applies the same copy rule, so a copy and its original
  are never a clarification.
- **The receiver pass binds the class the calling file can see** (`index._visible_class`): its own
  file's, one from a file it imports, then one under a top-level folder it imports from or lives
  in; never across the boundary of a copy or reference tree. Sidecar v3 (older per-file facts are
  reused).
- **One build at a time** (`buildlock`): an operating-system file lock on `.verinoda/build.lock`
  around `scan` and `update` and so around every refresh (analyze, verify, claim add, feedback,
  decide check, `ui --watch`, MCP). A second caller waits (the CLI up to 10 minutes, saying who
  builds; MCP `index_update` 30 s; `decide check`'s refresh 120 s, MCP `decision_check` 30 s;
  `analyze --refresh inline` like the CLI) or, asked not to wait (analyze on a project of 300+
  files in `auto` mode, `ui --watch`), does nothing and returns `mode: busy` with the holder.
  It is never told to use `--force`; a rebuild that failed (`Rebuild failed: ...` in the indexer's
  log) is no longer reported as a refused shrink either. A gate never passes on edges it could not
  read: when `decide check`'s refresh fails or the build is still running after the wait, every
  no_edge guard is `unknown` (`graph_stale` says why; a violation still found on the old graph
  stands, as its line is re-read) and without a violation the exit is 2.
- **Freshness on every read** (`freshness.check`): query, trace, `map`, MCP `project_query`,
  `node_inspect`, `relation_trace` and `map_view` say "N file(s) changed since the index" with the
  list (`stale_count`, `stale_files`). The check lists each folder that holds indexed files once
  and hashes only a file whose size or time moved (the `file_stat` cache). A new file counts only
  when the snapshot's own rule would list it, so `verinoda update` can always clear what is
  reported: git must list it (`git ls-files --cached --others --exclude-standard`, the paths as
  literal pathspecs after `--`; a file inside a nested repository or a submodule is not listed), an
  untracked file under build/ or dist/ is build output, and a folder holding a `.git` is not
  looked into (asked once per snapshot and path, `fresh_ignored.json`; also for a project inside a
  larger repository, whose ignore rules `list_files` follows too). A code name of a query that
  no node has but a changed file spells, where the version the index describes did not spell it
  (read from the search index's tokens of that file, when it holds the snapshot's version), is
  reported first ("not in the index yet ... `x` at file:line ... the passages below are not about
  it"); when that version could not be read the line says "may not be in the index yet" and does
  not disown the passages; a name the indexed file already spelled (a constant in a file with an
  unrelated edit) is not reported at all and trace calls it `not_a_symbol` with the stale note.
  trace never resolves such a name by similarity, and neither `node_inspect` nor impact offers a
  similar name as a candidate for it.
- **analyze's refresh is not the answer's time.** The refresh runs first and its time is given
  back to the budget. On a project of 300 or more files, a refresh that would rebuild the graph
  and is expected to take over 15 s (the last recorded graph build, `build_stats.json`; without
  one, 0.015 s a file), or that faces over 200 changed files or another build, is not run: the
  answer comes from the previous index, `index_refresh` and an unknown name the stale files, and a
  claim citing one says so. A changed file that spells a sub-question's subject (its linked
  symbols' names, a name written as code) may hold what was asked (a new caller) and was not read:
  each is an unknown of its own ("verinoda/textnorm.py:297 spells `split_identifier` ... not in
  this answer", a use preferred over the definition line) and the sub-question is at most
  `met_with_inference` (`flags.stale_subject`). The MCP server then starts `verinoda update` in a
  separate process (a thread would share the process-wide stdout redirection with the protocol
  channel), one at a time; while it holds the lock, the tools keep answering from the graph
  already loaded. That process runs the server's own Verinoda in isolated mode (`python -I -c`
  with the package's parent first on `sys.path`, from the temp folder): the first version ran
  `python -m verinoda` from `<repo>/.verinoda/index`, so a repository shipping
  `.verinoda/index/verinoda/__main__.py` had its own code executed.

### 10.3 Measurements

See BENCHMARKS.md, "Update 2026-09-26: exact names and a fresh index". The eight benchmark sets
are unchanged (every question and approach equal to the main run of the day); the agent persona's
ten out-of-sample questions are compared there too. The review fixes (section 10.5) were measured
again the same way.

### 10.4 Not done / limits

- The refresh is still a full graph rebuild whenever a code file changed (gap 5): analyze no
  longer waits for it on a big project, but `update` itself is as slow as before.
- trace and `node_inspect` still resolve plain words by similarity (with the `fuzzy` note or a
  heuristic resolution and candidates); impact never does. A module constant or an attribute is
  not a node, so `trace x config.LIMIT` is now unresolved with hints where it used to trace the
  similar node; pass the file or the function that reads it.
- New files in a folder the index does not know are looked for up to 200 files; outside git no
  ignore rule applies. Whether a name is new to a changed file is read from the search index's
  tokens: a compound identifier is kept whole, a one-word name only by its stem.
- The CLI `analyze` does not start a background update (it says to run `verinoda update`); the
  lock covers Verinoda's own builds, not a graphify CLI run through `verinoda index --`.
- Ambiguity is decided among exact names only; a name defined in two files of the product is now
  `ambiguous` in trace where it used to pick one, and so is a top-level function next to a method
  of that name (`get_event_loop` in asyncio: two policy methods and the module function).
- `not_a_symbol` names the first site `name_site` finds, which can be in a detected copy.
- `map --view dataflow` lists the project's own entry points before a copy's or reference tree's,
  but example and fixture code of the project (examples/orders_app on Verinoda's own tree) still
  counts as the project's own there.

### 10.5 Review of the branch (reviewer-b, 2026-09-26)

A review of the first version (eleven findings, with repros on a copy of Verinoda's repository and
of the CPython standard library, 79,535 nodes) found, and the fixer changed:

- **The tie-break still answered about the wrong symbol** (high): "no method in-edge" counted a
  test's nested helper as top-level, and "a label without `()`" beat real functions across files.
  Now the rules of 10.2 apply; `call_soon` and `call_later` are `ambiguous` between asyncio's two
  methods (`map --target call_soon` exits 2), `_run_once` resolves to `BaseEventLoop._run_once`
  with the two test definitions set aside, `update` on Verinoda's tree is ambiguous among five
  product definitions (three copy and one Apex-fixture definition set aside).
- **Security** (high): the background update imported a `verinoda/` package the project ships (see
  the analyze bullet); a test plants one under `.verinoda/index/` and at the root and runs the real
  spawn.
- **"Not in the index yet" for names the index had** (medium): see the freshness bullet.
- **Stale files the snapshot never lists** (medium): see the freshness bullet; on the reviewer's
  repros (a nested clone of a small git repository in Verinoda's tree; untracked `build/lib/foo.py`
  and `dist/pkg-1.0.tar.gz` in the standard library copy) the check now reports 0 (it reported 5
  and 2, and `update` could not clear them).
- **A callers answer `met` while a changed file held a new caller** (medium): see the analyze bullet.
- **Cold trace and impact 1.5-1.8x slower on a large graph** (medium): an exact lookup built
  question_plan's linking index (stems of every node's identifier parts, about 4 s on 79k nodes)
  and an unknown name ran the fuzzy scorer twice. Exact names now use a lookup built in one pass
  (folded bare labels, file nodes, file stems: 0.3 s there), the scorer runs only for plain words
  and at most once per text, and the local-definition rule reads a Python file's own parse. The
  numbers are in BENCHMARKS.md.
- **`map --view dataflow` flooded with the detected copy** (medium): its entry points are now the
  project's own first, then those in a detected copy or reference tree (marked `"in"`), whose
  paths only fill what `max_paths` leaves; on Verinoda's tree none of the 20 paths starts in the
  copy (all 20 did).
- **decide check did not wait for a running build** (low) and **`analyze --refresh inline` did not
  either on a big project** (low): see the build bullet.
- **node_inspect and impact offered a similar name for a `not_indexed` one** (low) and **the text
  output hid the node a name resolved to** (low): see the resolver and freshness bullets.

## 11. JVM callbacks and mod entry points (D38, 2026-09-26)

### 11.1 Findings that drive the design

- Mod code is callback-driven. `createTickerHelper(..., EmberForgeBlockEntity::serverTick)`,
  `END_SERVER_TICK.register(RepairScheduler::tick)`, `registrar.playToServer(..., EmberNetwork::handleStoke)`:
  the graph had no edge for these, so `trace EmberForgeBlock.getTicker EmberForgeBlockEntity.serverTick` and
  `trace RepairScheduler.register RepairScheduler.tick` found no path, and impact on `serverTick`,
  `handleStoke` or `RepairScheduler.tick` was empty (a change there looked safe).
- `map --view dataflow` was empty on both example mods: no framework entry points, no JVM sinks.
- `claim add "RepairScheduler.register calls RepairScheduler.tick" --kind relation` on the method reference
  line got `strong_inference` 0.70, the grade of a real call whose receiver is not resolved.
- A first attempt (d70b801, reverted in 3c066ec) made method references `calls` edges. It moved benchmark
  scores (forge_mod JSON 50 -> 46, glow_mod analyze 43 -> 42), let a method reference hide a later direct
  call to the same method, became a verified `calls` claim with a SCIP index, resolved `this::m` inside
  anonymous classes and Kotlin objects to the outer class, and read Java text blocks as code.

### 11.2 Decisions

- **A separate relation, `registers`** (`index.java_registers_edges`): from the method that passes
  `Cls::m`, `this::m`, `var::m` or Kotlin `::m` as an argument to the referenced method. The class is resolved
  as for `java_call_edges` (an import, the file's package, a `pkg.*` import; else no edge); `var::m` follows the
  declared type; `this::m` and Kotlin `::m` only where `this` is the named class that holds the method (a brace
  scan of the code: not in an anonymous class, a Kotlin object expression, a companion object or a Kotlin
  lambda, which may have another receiver); `::new`, `::class`, a dotted receiver, a reference that is not an
  argument, a field initializer or static block (no method) make no edge. Comments, strings, Java text blocks
  and Kotlin raw strings are blanked first. Edge data: `INFERRED`, `_origin=verinoda.java_refs`, `context`
  ("RepairScheduler::tick passed to ServerTickEvents.END_SERVER_TICK.register(...)"), `registrar`,
  `source_location`. Stored in the receiver-call sidecar under `registers` (version 3, so old sidecars are
  recomputed); `_apply_edges` keeps one edge per relation, so a `registers` edge and a direct call between the
  same two methods are both kept.
- **The ranking does not see it**: `search_index.REL_WEIGHTS` (graph prior) and `retrieval.EXPAND_RELATIONS`
  list the relations they weigh; `registers` is in neither. `analyze` builds claims from `calls` / `uses` /
  `inherits` edges only.
- **trace** follows `registers` only when there is no path without it, so every path found before is
  returned unchanged; the hop is `relation=registers`, `kind=callback`, with its `context`, and the result has
  a `note` (the framework calls the method later; it is not a call at that line). `analyze` passes
  `callbacks=False`: its flow claims state calls.
- **Impact** (`architecture_map.impact`, the UI's impact, review dependents) walks the other relations
  first, then continues through `registers` edges; the nodes found before keep their distance, the new ones
  are marked `registers (callback)`.
- **Claims**: on Java / Kotlin, a cited line whose only mention of the target is a method reference is
  call-site code `registers` (grade none): "A calls B" on it is `weak_inference`, below a real call, and
  `claim add` attaches the line as qualifying evidence with that reason. A `registers` graph edge and a
  resolver's definitive answer (SCIP finds the method a reference names) on such a line are graded the same
  way: never a verified call.
- **Entry points** (`architecture_map.framework_entries`, heuristics over the text, each with its reason and
  a `basis`): `declared` - fabric.mod.json `entrypoints` (`main`, `client`, `server`, `fabric-gametest` ...,
  `Cls::method` too), classes implementing `ModInitializer` / `ClientModInitializer` /
  `DedicatedServerModInitializer` (their `onInitialize...`), `@Mod` classes (their constructor); `framework` -
  `@SubscribeEvent` methods with the event type, `@EventBusSubscriber` classes, mixin handlers (`@Inject` ...
  into the `@Mixin` target), methods registered with an event field's `register` or a bus's `addListener`;
  `callback` - methods handed by reference to a known game registrar (`createTickerHelper`, `playToServer` /
  `playToClient`, `registerGlobalReceiver`, brigadier `executes`). A method handed to `forEach`, `map` or
  another call that runs it at once is no entry point. They come before the name heuristics. Test source sets
  are left out.
- **JVM sinks** (`JVM_SINK_PATTERNS`, on comment- and string-free lines, `derived_by` says heuristic):
  `Files.write/writeString/newBufferedWriter/newOutputStream/copy/move/createFile`, `new FileWriter` /
  `FileOutputStream`, `NbtIo.write*`, `setDirty()` / `markDirty()` / `setChanged()`. Kept apart from
  `SINK_PATTERNS`, which review reads.

### 11.3 Measurements

- Edges: forge_mod 2 (`getTicker -> serverTick`, `EmberNetwork.register -> handleStoke`), glow_mod 8
  (tick, stopping, chunk-load and client-tick events, the packet receiver, three command handlers). Entry
  points: forge_mod 10 (1 declared, 7 framework, 2 callback), glow_mod 10 (2, 4, 4). Dataflow paths: 0 -> 2
  on each (glow: `onInitialize -> GlowConfig.load` writes the config file; forge: `handleStoke -> stoke`
  sets the dirty flag, `serverTick` sets it itself).
- Both trace cases now return the callback path; impact on `RepairScheduler.tick` lists
  `RepairScheduler.register` at distance 1 (`registers (callback)`).
- fastbench on the prepared corpora (sidecars recomputed, `registers` edges counted in the run): 0 fact and
  0 negative changes on all nine sets (BENCHMARKS.md).
- Cost: the pass takes about 1 s on the largest mod corpus (computed once, stored in the sidecar); the
  dataflow view about 0.5 s more there.

### 11.4 Not done / limits

- `analyze` does not build flow claims through callbacks (it asks trace for call paths only).
- A lambda passed as a callback (`EVENT.register((a, b) -> ...)`) is not an edge; its body's calls belong to
  the enclosing method already. A reference inside a lambda or an anonymous class is attributed to the
  enclosing graph method.
- A method reference in a static initializer block or a field initializer has no method to hand it over, and
  a target inherited from a superclass is not resolved; overloads are one graph node.
- `java_call_edges` itself still reads a Java text block as code (unchanged here; changing it moves the
  calls pass).
- Entry points and sinks are text heuristics: an annotation spelled through an alias or a registration made
  through a project helper that takes a lambda are not seen.

## 12. Honest verdicts (D39, 2026-09-26)

### 12.1 Findings that drive the design

- A senior review with five personas found `analyze` saying `met` for irrelevant or incomplete answers: 4 of
  6 judged backend questions, 4 of 10 JVM questions, 2 of 5 of the lead's. "What calls
  `BaseEventLoop.call_soon`?" was met with 1 caller where the code has 7 `self.call_soon` and 43
  `_loop.call_soon` sites; "how does update decide which claims become stale?" was met with two env-var reads
  from the frozen benchmark copy inside the repository; "where is the Wisp ticked?" was met though `Wisp` has
  no `tick`; "which mixins are not listed in glowmod.mixins.json?" was met with one right and one wrong class;
  "why are MCP tool calls serialised with a lock?" was met with a commit line; "which components use useAuth"
  was met with definitions; "what calls openPalette" missed the keydown-listener caller.
- `judge` checked the kind and strength of the claims (a verified definition, a verified relation), not
  whether they were about what was asked. D31's `off_subject` covered only definitions of items the search
  ranked near the question.

### 12.2 Decisions

- **D39. A verdict gate** (`verinoda/verdict_gate.py`), run in `analyze` after every claim is made
  and critique has run, so it costs no claim and no budget. It never drops a claim and never raises a verdict:
  it marks claims that cannot make the sub-question `met` (`flags["weak"]`) and gaps that keep it at most
  `met_with_inference` (`flags["capped"]`), and says why in an unknown. Both flags are stored with the
  analysis, so `plan audit` judges the same.
- **Relevance.** A definition ("X is defined at ...", "file:line contains ...") answers only a locate/define
  question (and a config one); for other intents it never makes a sub-question met. A locate question that
  asks which code uses X ("which components use the useAuth hook?", "hangi bileşenler kullanıyor") needs a
  definition of code that calls or uses X in the graph. "Where is X ticked?" (tick, draw, paint, render,
  mount: lifecycle callbacks) needs a member of X named for it (`EmberForgeBlockEntity.serverTick`); other
  verbs are left alone because the code that does them is seldom named after the thing, and a subject that
  points back ("those registers") is not read. A callers answer must be about the callee the question names or
  links, not a symbol ranked near it. A configuration read must carry one of the question's words or their
  expansions in the variable, the reading function or its file.
- **Copies.** Claims whose evidence lies only under an `index.reference` tree, a folder `verinoda.copies`
  detected as a copy of the project, or a vendored folder (`vendor/`, `third_party/`, `node_modules/`, ...)
  never make a sub-question met, unless the question names the tree (the same rule and aliases as the
  search ranking).
- **Unresolved call sites.** For a callers question the indexed code files are searched for the callee's name
  written as a call, a method reference (`Cls::name`), a callback argument (`addEventListener("keydown",
  name)`) or a JSX handler. A site counts when the graph tied neither the line nor the function around it to
  any definition of that name, and its receiver can be the callee (not another class, not `self` for a module
  function, not an instance or a `new Other()` for a static member). Any such site caps the verdict and the
  unknown says "N call sites unresolved: file:line, ..." (up to four). The search stops after 8,000 files or
  96 MB (a count, so the same tree gives the same answer) and says so.
- **Why.** A commit line says when code changed, not why. History claims count only when the commit subject
  states a reason ("because", "to avoid", "çünkü", ...); without a decision record that explains it the verdict
  is at most `met_with_inference` and an unknown says the reason was not found.
- **Set difference.** "Which X are not listed in Y", "which ... are missing / unused / have no ...", Turkish
  "hangi ... listelenmemiş / olmayan / eksik / değil" are `not_supported` with the reason: no handler computes
  a set difference. The test sits in `_exclusive_guard`, beside D31's "is X only called in Y?" (the other face of
  the same question) and uses its verdict path.
- **Measurement** (`verinoda benchmark verdict-audit`, `benchmarks/verdict_audit/`): 17 traps and 22 controls on
  public material (the three examples, three fixtures under `tests/fixtures/verdict_audit/`, this repository at
  `343a00d`), split into dev (23) and held-out (16) before any rule was written. `wrong_met` = met while the
  answer misses a gold string, holds a wrong one, or the true answer is an absence; controls measure
  over-refusal.

### 12.3 Measurements (2026-09-26)

| split | wrong met before | wrong met after | controls kept before | controls kept after |
|---|---|---|---|---|
| dev (23: 10 traps, 13 controls; rules tuned here) | 9/23 | 0/23 | 13/13 | 12/13 |
| held-out (16: 7 traps, 9 controls; first run with the rules frozen) | 8/16 | 1/16 | 7/9 | 7/9 |

- The dev control lost (`pyloop-why-adr`) was met before only through a commit line; its ADR claim is graded
  `weak_inference` because the quoted line holds a negation ("never inside the call") and entail's polarity
  check counts the claim's framing words ("Decision record ... explains it") as terms of that sentence.
- The held-out wrong met left is a control, wrong before as well: "Where is max_heat declared?" is met with the
  TOML default and `getMaxHeat`, and the Java declaration is not found. No rule here sees a missing definition.
- No gold fact is lost on the nine fastbench sets (0 of 333 set x question x approach cells change). On the
  eight public sets 74 sub-question verdicts were met before and 68 after; the six now `met_with_inference` are
  four "why" questions answered only by a "benchmark corpus" commit line, a "which code uses these recipes"
  answered by definitions, and a callers question answered with callers of another function. The gate takes
  a few milliseconds per question; the call-site search about 0.2 s on the 226-file Graphify set (0.6 s under load).
- In-sample caveats: the rule author wrote both splits, and the trap shapes come from the review's list.

### 12.4 Not done / limits

- The checks are lexical. A synonym ("opened" for `connect`) or a subject the code names differently is not
  matched, which is why the action rule is limited to lifecycle callbacks. A reason stated in a comment or a
  docstring next to the code is not searched for (the lock's reason is in `mcp/server.py`'s docstring): the
  "why" question is capped, not answered.
- The call-site search reads text: calls through another name (an alias assigned at run time, `getattr`, a
  string) are not found, and for a method any lowercase receiver counts (an instance of another class with a
  same-named method caps the verdict too).
- Set differences are refused, not computed (mixin configs, registries, locale keys would be the first).
- The ADR negation grading above is an entail rule (D31), outside this gate; fixing it would raise verdicts and
  was left to that rule's owner.

## 13. Query ranking (D40, 2026-09-26)

### 13.1 Findings that drive the design

The senior review (gap 10, high) found that `verinoda query` ranks tests first and ignores module names on
code outside the benchmark sets:

- CPython standard library copy, 8 new questions: the right answer first in 2, in the top 3 in 3, missing in 3.
  "where does subprocess on Windows build the command line string" gave `test/test_cmd_line.py`, `cmd.py`,
  `pdb.py`, not `list2cmdline` (whose docstring says "command line string").
- Verinoda's own repository: a test at rank 1 or 2 in 4 of 4 questions. Upstream Graphify: a test first even with
  `graphify/cluster.py` written in the question.
- Out of sample (agent persona): 127 of 201 listed standard-library items were tests. `http.client.HTTPConnection.request`
  gave every symbol named `client` the score of a named symbol, and `client -> cli`, `request -> req` prefix
  expansions flooded the list.
- A large Minecraft mod (private): a test class first for a "how does X work" question; an inflected Turkish
  word expanded to unrelated names that begin with its four-letter stem, and to a three-letter prefix of it.
- A flat 0.8 factor on test files had been tried and reverted (2026-09-24: heldout +3, glow_mod -2,
  orders_app_tr -1 facts).

Why: test names repeat the words of the code they test (`test_update_after_edit_marks_claim_stale_via_cli`,
`TestTemporaryDirectory.test_del_on_shutdown`), and BM25F's name field rewards that. The path field (weight
0.5) is too weak for a module the question names, and a dotted name's module parts were taken for symbol names.

### 13.2 Decisions

All in `search_index.rank` and `analyze_query`; no index change (no re-index needed).

- **Tests yield to the code that matches the same words** (`_tests_yield`). For a question that is not about
  tests and not about callers or the impact of a change, a test function or class among the 180 best-scored
  units moves just below the best non-test code unit that scores less than it, but at least 0.7 of its score,
  and matches at least 0.75 of the test's matched question words (weighted by idf; a joined pair such as
  `temporary_directory` stands for both of its words). A test with no such unit keeps its score: it is the only
  place those words meet, or far ahead of any code that has them. It runs after the graph prior, so tests still
  seed the PageRank that reaches the code they call. Module-level blocks of test files (fixture text) keep their
  rank.
- **What the question asks** (`asks_about_tests`, `asks_about_callers`, English and Turkish). A question about
  tests (tests, coverage, `HeatMathTest`, `hangi testler`) or about callers / impact (who calls, what calls,
  affected, retest, `if ... changes`, `kimler tarafından çağrılıyor`, `değişirse`): tests keep their rank there,
  since they are the answer, or callers and what to re-run. A question about tests also gets the test file of a
  module it names (`shutil.copytree` -> `test_shutil.py`).
- **Files and modules the question names** (`_mentions`). Units of a file the question writes as a path
  (`graphify/cluster.py`, `HeatMath.kt`) or a dotted module (`http.client`, `search_index.rank`, `json.dumps` ->
  the `json` package) score x2.0, and the result says "question names ...". The module parts of a dotted name are
  no longer symbol names (`client` in `http.client` is not every `client()`); each later part is owned by the one
  before it (`HTTPConnection` by module `client`). A plain word that is the stem of at most 3 code files that are
  not tests, or a Python package (`subprocess`, `logging's`), scores x1.3 with no reason line. A folder or package
  that holds more than a quarter of the indexed files (the project itself) names nothing. The factors apply after
  the scale is set, so a boost does not push every other unit down.
- **Docstring phrases** (`_doc_phrases`). A non-test symbol among the 300 best whose docstring (first paragraph)
  writes two adjacent question words side by side (one filler word allowed) scores x1.25 per pair, at most two
  pairs. Test docstrings do not count (they state the scenario in the code's words), and a question about tests
  gets no docstring boost.
- **Narrower expansions** (`analyze_query`, `_stem_terms`). No prefix or abbreviation expansion of an expansion
  (`paramet -> param`). No corpus-prefix expansion of a word the code itself names things with (`client`,
  `request`, `connection`), unless the rest is also a name (`emberforge` -> `ember` + `forge`). No corpus-prefix
  expansion of an inflected Turkish word (`parayı` -> `par`). A four-letter Turkish stem reaches a corpus term only
  when the rest is Turkish inflection (`textnorm.TR_SUFFIXES`), not the looser suffix chain that also took single
  consonants (`parameter` and `parallel` for `para`).

### 13.3 Measurements

A dev set of 37 questions (16 on a CPython `Lib` copy, 5 on upstream Graphify, 11 on Verinoda's own repository at
343a00d, 5 on the examples; 3 ask about tests), written for this change with gold files and symbols; scored with
the query as `verinoda query --json` returns it (items, then `budget.more`). Base 343a00d -> this branch:

| metric | base | branch |
|---|---|---|
| gold file first | 13/37 | 28/37 |
| gold file in top 3 | 25/37 | 34/37 |
| gold file in top 10 | 32/37 | 36/37 |
| MRR (gold file) | 0.535 | 0.836 |
| gold symbol in top 3 | 19/35 | 27/35 |
| tests in the top 5, 34 questions not about tests | 61/170 | 26/170 |
| questions whose first result is a test | 10/34 | 1/34 |

The dev set is in-sample: the rules were written while looking at it. A held-out set written by someone else is
run after this change (not reported here).

Fastbench (9 sets, 333 set x question x approach rows, against integrate/0925): no fact lost, 2 gained
(`heldout_repoatlas` h05 JSON 0 -> 1, `orders_app_tr` q10 JSON 1 -> 2), negatives unchanged. On the private mod
set the facts are unchanged, and a test is ranked first for 1 of its 16 questions not about tests (4 before).
Warm `retrieve` time on the CPython copy and Verinoda's repository did not change beyond noise (median
0.30-0.35 s vs 0.27-0.31 s, and 0.250 s vs 0.246 s).

Variants measured and not kept (fastbench against the same baseline):

- tests multiplied by 0.7 when covered (instead of moving just below the covering code): dev a little better
  (top 1 29/37, tests in top 5 20/170), fastbench -4 facts (`heldout_repoatlas` h02 x3, `orders_app` q01 JSON)
  and +5;
- code yielding to tests on questions about tests: -1 (`graphify_core_tr` g09 JSON: the code item carries the
  `called_by` list of the tests);
- docstring boost on tests: a test ranked first on `heldout_repoatlas` h02 (-2);
- boosts before the scale was set: -1 (`graphify_core` g01 JSON);
- no prefix expansion of an identifier word at all: -1 (`forge_mod` q03 JSON, `emberforge -> ember`).

### 13.4 Not done / limits

- A plain word that is also an English word names its module when a file has that stem (`string`, `copy`,
  `select` in the standard library): x1.3 only, but it is noise there.
- Test files are what `is_test_file` says (the night/test-predicate branch replaces it); a test helper outside
  a test folder is code.
- A dotted name whose first part is a class (`Wisp.spawn`) boosts no file; the owner rule picks the method.
  A lowercase owner still accepts a method of any class in that module or package (`asyncio.run` also matches
  `REPLThread.run` in `asyncio/__main__.py`).
- Vocabulary gaps stay: "allowed" does not reach `allowlist`, "choose a temporary directory" does not reach
  `_get_default_tempdir`, and a Turkish question about the search index still finds the Turkish overview first.
- Copies of the project inside it (upstream Graphify's `worked/mixed-corpus/raw/`) still rank next to the real
  code unless the question writes the path.

## 14. Documents and images next to the code (D41, 2026-09-26)

### 14.1 Why

An outside comparison with Graphify listed as a gap that Verinoda leaves out a repository's PDF reports, design
documents and screenshots. Graphify reads them through an LLM subagent; Verinoda read none of them (a `.pdf` was
"binary"). A project's decisions and specifications often live there.

### 14.2 Decisions

- **A text view per file, made the same way every time** (`doctext.py`): PDF through pypdf (added as a
  dependency; pure Python), `.docx`/`.xlsx`/`.pptx` with the standard library (zip and ElementTree; zip bombs and
  XML entities refused before reading), with a heading line per page (`# Page 3: <its first line>`), sheet, slide
  or Word heading. Because the view is deterministic, evidence quoted from it can be checked again, like a
  Markdown line. Views are cached per content hash under `.verinoda/index/doctext/`.
- **Images through the OCR engine built into Windows** (`Windows.Media.Ocr`, run by a PowerShell script shipped
  in the package): no model, no download, no network. Texture, icon and font folders, images under 4 KB or with
  a side under 120 px (or both under 300 px), and images past 300 per run are skipped with the reason. One OCR
  process per run; results cached by content. `VERINODA_OCR=0` turns it off. OCR text is what the engine read.
- **Documents are first-class**: the markdown extractor reads the view, so pages and headings are graph nodes
  (a new document now triggers a graph rebuild, as a new Markdown file also does since this change); the
  search index, the lexicon (so `analyze` does not call a document's words absent), evidence (`design_doc`),
  anchors (Markdown sections of the view) and spans read the view. The view's format version is part of the
  graph's extraction cache key for documents.

### 14.3 Measured

- A sample project with a PDF, a Word file, a spreadsheet, a deck and a screenshot: `query` finds the PDF page
  and the OCR'd screenshot (a Turkish question found the Turkish text in the image and the code it names);
  `analyze "what is the payment retry backoff policy"` answers `met` with the PDF page as a
  `statically_verified` claim; editing the PDF makes that claim `stale` on `update`.
- Fastbench on freshly built indexes (the code before and after, each building its own): 0 differences over the
  9 sets (333 question x approach cells each).

### 14.4 Not done

Scanned PDFs (no text layer), audio and video, OCR on macOS and Linux, and diagrams' structure (only their text).

## 15. A faster update with the same graph (D42, 2026-09-26)

### 15.1 Findings

Profiling `update` after a one-line edit on Verinoda's own repository (about 2,400 files): of 31 s, extraction
and the cross-file passes took 8.7 s, community detection 3.4 s (networkx's Louvain, because the native Leiden
package was an optional extra), the file walk 2.7 s, the derived indexes 3.4 s, and writing the 34 MB graph.json
twice with `indent=2` (Python's pure encoder) about 2.5 s. The path helpers were called 1.4 million times.

### 15.2 Decisions (the output does not change)

- `graspologic-native` (abi3 wheels for Windows, macOS and Linux) is a dependency: Leiden runs in native code
  (clustering 3.4 -> 1.0 s). A simple graph's edges are ordered by their endpoint pair only (the attribute
  JSON per edge and per split pass was a second; a pair is unique in a simple graph).
- graph.json is written on one line through json's C encoder (still JSON, same content).
- Memoised: a module's stem per node id in the Python member-call pass (350,000 calls), parent path parts in the
  proximity tie-break, English stems in the lexicon's association pass.
- Checked: the same graph.json content, community labels, lexicon and receiver-call edges from the old and the new
  code on four corpora (a 106-file copy, the examples, Verinoda itself, a 4,690-file Java mod).

### 15.3 Measured

The real CLI, the same edits on two identical copies, alternating, both with Leiden installed: 31-38 s before,
27-33 s after (about 12%); a user without the Leiden extra also saves the clustering difference (about 2.4 s
here). Earlier, in the first measurement on one copy: 31.3 -> 24.0 s.

### 15.4 Not done

An update proportional to the change: the vendored cross-file passes (about 20 of them) read every file's
extraction; making them incremental without losing edges is the next step.

## 16. Name check for Java (D43, 2026-09-26)

### 16.1 Why

`verinoda check` was Python only, so invented APIs in Java - the language of Minecraft mods, where an agent
trained on older mappings writes `PlayerEntity`, `getMainHandStack` or `spawnEntity` for `Player`,
`getMainHandItem` and `addFreshEntity` - were never caught; a wrong Mixin target only fails at game start.

### 16.2 Decisions

- **Read what the build compiles against, never run the build**: `jvmclass.py` parses class files (constant
  pool, super types, methods with parameter count, varargs, static and erased return type, fields, member
  types) from the classpath and the JDK's `lib/ct.sym` for the build's release. The classpath is the configured
  one or the one a Fabric Loom build left (its run argument files, the Minecraft jars and Minecraft's own
  libraries); nested jars are read. Cached per jar version.
- **Closed world as in the Python check**: `absent` only when the type and all its super types are read and the
  package the name would come from is closed. Java's static typing makes a declared type closed (a method its
  hierarchy lacks does not compile). Everything else is `unknown` with the reason.
- **One classpath per build**: a repository can hold several builds (a Fabric mod and a Bukkit plugin); a file
  belongs to the outermost folder with a Gradle settings file, else the nearest with a build file.
- **Mixin targets** are checked against the target class's own members (a Mixin reaches only those); an
  inherited one is `unknown`.

### 16.3 Measured

- A real Fabric mod for Minecraft 26.2 (unobfuscated names; 150 jars, 45,223 library classes, the JDK 25 API):
  407 files, 126,848 sites, 0 absent (it compiles), 2,981 unknown (2.3%), 4.7 s warm.
- 8 of 8 planted mistakes caught with the right name as the nearest: `PlayerEntity` (Player), `getMainHandStack`
  (getMainHandItem), `spawnEntity`, `new ItemStack` with 4 arguments, `Items.DIAMOND_SWORDD` (DIAMOND_SWORD),
  `getCount(5)`, `substring` with 3 arguments, `@Inject(method = "tickk")` (tick).
- The example mods without a classpath: 0 absent (library names unknown, as they should be).
- Bugs found on the way and fixed before release: enums' implicit `values()`/`valueOf()`, a package named
  `build`, comments counted as arguments, `Outer.this`, Mixin targets written with `+`, local records, member
  types inherited from unread super types.

### 16.4 Not done

Kotlin and Groovy sources, argument types and overload resolution by type, visibility, a Maven classpath, and
the classpath of a Gradle build that is not Loom (configure `code_check.classpath`).

## 17. The changed files now, the graph in the background (D44, 2026-09-26)

### 17.1 Why

After D42 an update of Verinoda's own repository still took 27-33 s, all of it spent rebuilding the graph:
the cross-file passes, communities and the graph file read the whole corpus. An agent that calls
`index_update` after every edit waits that long each time. Making every pass incremental without changing
the graph is a large rewrite of the vendored pipeline (about 20 cross-file passes; communities would drift
from a full scan's).

### 17.2 Decisions

- `update --fast`, when the graph would be rebuilt, takes in only the changed files: the search index (a
  changed file's units follow the new text; the graph's spans for it are older, so the file is marked
  misaligned and re-indexed by the next build), the lexicon, the syntax facts and the stale claims (checked
  against the working tree's hashes, as a refused rebuild does). Then it starts `verinoda update` in a
  detached process (`workflow.start_background_update`: the isolated child the MCP server already used, now
  shared through `buildlock.updater_argv`), after releasing the build lock.
- **No snapshot until the graph is built**: every reading command keeps saying which files changed since
  the index (`freshness`), and the result lists `graph_behind`. Nothing presents the older graph as current.
- The background build is a plain `update`: the graph is the one a full scan makes.
- MCP `index_update` takes the fast path when the last graph build took more than 15 s (the threshold
  `analyze` uses to decide whether to refresh inline). The CLI keeps the full update unless `--fast` is given,
  so scripts and CI see the graph they asked for.

### 17.3 Measured

Verinoda's own repository (about 2,400 files, one function added to a module): `update --fast` 3.4 s (4.3 s
with the process start) against 27-33 s for `update`; the new function was found by `query` at once (as a
module-level passage, with "may not be in the index yet"), and after the background build as a symbol with its
call edge. The background build's graph and a `scan --force` afterwards: the same 29,039 nodes and 68,611
edges, communities included.

### 17.4 Not done

The graph build itself still reads the whole corpus, so the graph lags by one build's time after an edit.

## 18. Name check for Kotlin (D45, 2026-09-26)

### 18.1 Why

After D43 Kotlin files still came back `not_checked`, and Java code in a mixed project could not see the
project's Kotlin classes: a Java call on a Kotlin class of the same package could have been called absent.

### 18.2 Decisions

- **One world for the JVM**: the Java check's universe also reads the build's Kotlin sources
  (`codecheck_kotlin.declarations`: classes, interfaces, objects with `INSTANCE`, companions, enum entries,
  properties with their getters and setters, top-level functions in a `FileNameKt` facade, type aliases).
- **Kotlin names stay open where the language keeps them open**: a member not found is `absent` only when the
  receiver's type is a project or library class (not one of Kotlin's built-in types), every super type is read,
  no extension of that name exists (the project's, with the receiver types they extend; the libraries' from their
  file facades' static methods and their `@kotlin.Metadata` names, which `jvmclass` now reads), kotlin-stdlib
  itself is on the classpath, and the receiver was not checked with `is`/`as`/`when` earlier in the function.
  The number of arguments is never checked (default and named arguments).
- **A file tree-sitter-kotlin does not parse completely decides nothing**: its types are open, its package is
  not closed, its own sites are at most `unknown`. The same holds now for Java files that do not parse.
- Receivers: declared parameters and properties, constructor calls, `this`, string literals, casts, and the
  declared type of a call or property down a chain (a Java getter for a Kotlin property); `X::class` is a
  `KClass`, `x::name` a callable reference.

### 18.3 Measured

- A sample with 6 planted names (`stop`, `powr`, `strat`, `unregister`, `startt`, an import `maxx`) against the
  real kotlin-stdlib and JDK: 6 caught, the project's own extension and a companion's function found.
- kotlinpoet (86 files, 15,090 sites) with only kotlin-stdlib 1.9.10 as the classpath: first 293 absents; the
  causes were parse errors of the grammar, smart casts, `X::class`, companions of library classes, nested
  constructors and extension imports. After the fixes: 10 absents, all extensions of kotlin-reflect
  (`createType`, `starProjectedType`, `declaredFunctions`), a dependency the given classpath lacked.
- The Java mod of D43 unchanged: 126,848 sites, 0 absent; 8 of 8 planted names caught.

### 18.4 Not done

Argument counts, calls without a receiver, type inference beyond declarations and constructors (a lambda's
`it`, generic results), Kotlin script files, Kotlin/JS and multiplatform `expect`/`actual`.

## 19. Import check for TypeScript and JavaScript (D46, 2026-09-26)

### 19.1 Why

TypeScript and JavaScript files came back `not_checked`. The invented name an agent writes most often there is an
import: a helper file that does not exist, a package that is not installed, a hook from another major version
(`useHistory` after react-router 6), a misspelt export.

### 19.2 Decisions

- **Resolve as TypeScript does, read, never run**: relative paths with the TypeScript and JavaScript extensions
  and index files (a `.js` path finds its `.ts` source or its `.d.ts`/`.d.cts`/`.d.mts` declaration first),
  `tsconfig.json`/`jsconfig.json` `baseUrl` and `paths` (comments and `extends` handled), packages in the nearest
  `node_modules` up to the repository (never above it), their `types`/`typings`, `exports` maps (with `types`,
  `import`, `default` conditions and `*` patterns), `@types/<name>` before a package's JavaScript, a package
  imported by its own name, and `declare module "x"` blocks (from the project and `@types`; every declaration in
  such a block is exported). Node's built-in module names win over packages of the same name.
- **Closed only where the exports are all known**: a module's own declarations and export lists, `export *` chains
  that all resolve, `export =` of a namespace that is nothing else, CommonJS `module.exports = { ... }` and
  `exports.x =` (`unknown`, never `absent`, for a name a CommonJS module lacks). A file that does not parse
  completely, `export =` of a value, a bundler alias (`@/`, `~`, `virtual:`) and a missing `node_modules` give
  `unknown` with the reason. A default import is judged only against the project's own source files
  (`esModuleInterop` can give a package a default).
- **Imports only, and said so**: each checked file is also listed under `not_checked` ("imports checked; calls,
  members and types are not"), so `check` exits 4 on a TypeScript change unless something is absent (3) - a
  renamed function that is only called elsewhere is never passed as checked.

### 19.3 Measured

- A sample with real packages installed (react + @types/react, react-router-dom, zod, axios, lodash + @types,
  @types/node): `useEfect` (nearest `useEffect`), `useHistory`, a missing export of a project file, a missing
  file, `readFileSinc` (nearest `readFileSync`), an uninstalled package: 6 of 6. `AxiosErr`, lodash's `debounse`
  and a name behind zod's `export *` into a declaration file tree-sitter does not parse stay `unknown`.
- ky (67 files, 594 import sites, its own `node_modules`): 0 absent, 0 wrong `not_installed` (after fixing
  declared modules' implicit exports, a package imported by its own name, and a lookup that had found a
  `node_modules` above the repository).

### 19.4 Not done

Calls, members and types (they need the TypeScript compiler), bundler aliases from `vite.config` or `webpack`
configs, `.vue`/`.svelte` files, and `package.json` `imports` (`#internal`).

## 20. When a method runs (D47, 2026-09-26)

### 20.1 Why

"When does this run?" is the first question about a mod's method, and the graph could not answer it. A tick
handler registered with a method reference had a `registers` edge (D38), but most registrations take a lambda
(`UseEntityCallback.EVENT.register((player, world, hand, entity, hit) -> { ... })`,
`Scheduler.runLater(80, () -> finish(w))`), and the condition that decides whether the call happens at all
(`if (ticks % 5 == 0 && Settings.on("guard.watch", true))`) was only in the source. An agent had to read three
methods to learn "at the end of every server tick, every fifth tick, when the setting is on".

### 20.2 Decisions

- **Lambdas are registrations too** (`index.java_registers_edges`): what a lambda passed as an argument calls
  becomes a `registers` edge from the method around it, with `lambda: true`, the registrar, and for a scheduler
  (`runLater`, `runTaskLater`, `schedule`, ...) its first argument as `delay` (read as written: a literal the
  code text blanks is read back from the line). The extractor already has a `calls` edge for the same call: the
  `registers` edge says when it runs. Methods that call their lambda before returning (streams, collections,
  `Optional`, `getEntities`, `sendSuccess`, `computeIfAbsent`, ...) get no edge: the call is part of the caller's
  own run.
- **An event, in words** (`index.event_label`): Fabric, NeoForge and Bukkit events by name ("at the end of every
  server tick", "when a player right-clicks an entity", "before a living entity takes damage (it can cancel)"),
  a scheduler's delay ("80 ticks later", "after Math.max(1, t - 2) ticks"), the server or client thread, a
  future's completion. Anything else is said as what it is: "when prepare(...) calls it back". The label is
  computed when shown, never cached.
- **`verinoda when SYMBOL`** (`verinoda/when.py`): walks back over `calls` and `registers` edges (6 hops, 8
  paths); a path ends at a registration, at a method nothing in the project calls, or at the depth limit.
  Registrations come first. From a caller that both calls the method in a lambda and registers that lambda,
  only the registration is followed ("now" would be wrong).
- **Conditions from the code, not evaluated**: for each call, the `if`/`while`/`for`/`switch` blocks still open
  at the call line, a braceless `if (x)` on the line before, `else` as `not (x)`, and early exits
  (`if (x) return;`) in a block still open (a `continue` in a loop that has closed does not count). The
  structure is read from the text with strings and comments blanked, the condition itself as written, with
  its line.
- **In `analyze`**: "when does X run", "what triggers X", "X ne zaman çalışır / tetiklenir"
  (`question_plan.RUNS_WHEN`) is a callers question (not history, as "ne zaman" was). Its answer is the call
  claims on the way, one `flow` claim per registration ("`tick` runs at the end of every server tick:
  `initialize` hands it to `END_SERVER_TICK.register(...)` (file:line)") and one per guarded call ("`tick` calls
  `watch` only if ... (file:line)", its evidence the condition lines). Both are `strong_inference`: the event
  is read from the registrar's name, the conditions are not evaluated. Paths beyond two are an `unknown` whose
  next step is `verinoda when X`.

### 20.3 Measured

- On a private Fabric mod (4,584 files): 1,269 `registers` edges, 785 of them from lambdas; 159 carry a delay.
  The acceptance question (a guard method's "when does it run") answered from the tick registration in the
  mod's initializer and the every-fifth-tick condition, each with its file and line; before, the answer was one
  `calls` edge.
- fastbench on fresh indexes (9 sets, 333 question x approach results), old code against new: 0 differences;
  `registers` edges carry no ranking weight (D38), and a callers question keeps its answer unless it asks when.

### 20.4 Not done

Kotlin lambdas (method references only, as in D38), anonymous classes passed as listeners, events registered
through annotations (`@SubscribeEvent`) and the conditions of the registration itself (a handler registered
only in dev mode).

## 21. Mixin edges (D48, 2026-09-26)

### 21.1 Why

A Mixin handler is mod code that runs inside a game method: `@Inject(method = "checkSpawnRules(...)Z",
at = @At("HEAD"), cancellable = true)` on `MobMixin` runs at the start of `Mob.checkSpawnRules` and can make it
return false. The graph had one `references` edge from the handler to `Mob`, so "what stops mobs from spawning"
found neither the handler nor what it does, and `when` could not say when the handler runs.

### 21.2 Decisions

- **`verinoda/jvm_mixins.py`, from the annotations as written** (comments removed, strings kept): the `@Mixin`
  targets (`X.class` through the file's imports, `targets = "..."` strings), then every `@Inject`, `@Redirect`,
  `@ModifyVariable`, `@ModifyArg(s)`, `@ModifyConstant`, `@ModifyExpressionValue`, `@ModifyReturnValue`,
  `@WrapOperation`, `@WrapWithCondition`, `@Overwrite`: the target method (a descriptor is reduced to the name,
  `"a" + "b"` joined, a `static final String` constant of the file read), the injection point (`HEAD`,
  `RETURN`, `TAIL`, `INVOKE` with its target, `FIELD` ...) and `cancellable`. `@Accessor` / `@Invoker` name the
  target member (the value, or the method name without get/set/is/call/invoke).
- **Edges**: `injects` (and `accesses`) from the handler method to the target class node (the external class
  the file imports, or a project class), `EXTRACTED`, `_origin=verinoda.mixins`, with `kind`, `target_methods`,
  `at`, `at_target`, `cancellable` and a readable `context` ("@Inject into Mob.checkSpawnRules at HEAD - at its
  start; can cancel it"). Kept in the receiver sidecar with the other load-time edges (version 7). Nothing is
  checked against the target's bytecode: a wrong descriptor is reported as written. A target class with no node
  (a string target the file does not import) gives no edge.
- **Where it shows**: `query` prints `mixin: ...` under a handler and `runs: <event> (registered at file:line)`
  under a registered handler; `node` lists the edges; `when` ends a handler's path "inside Mob.checkSpawnRules,
  at its start; can cancel it"; `analyze` claims the injections whose target method the question's words (their
  translations included) name, the cancellable ones first when the question asks what blocks, prevents or stops
  something (`engelle`, `önle`, `durdur` in Turkish): "`MobMixin.guard$noSpawnInWard` runs inside `Mob.checkSpawnRules`
  at its start; it can cancel it (@Inject at file:line)", `strong_inference`, its evidence the annotation lines.
- **Seed**: `engelle` -> block, prevent, cancel.

### 21.3 Measured

- A private Fabric mod: 39 injector edges from 28 Mixin classes, every one with its target method and point
  after reading `+`-joined descriptors and `String` constants (before those two: 7 with a broken or missing
  method). The acceptance question, in Turkish ("what blocks creatures from spawning"), answered with the
  `checkSpawnRules` HEAD injection; before, the answer was the definitions of a class named after the word
  "creature".
- fastbench on fresh indexes (9 sets, 333 results), old code against D48 and D49 as committed: 0 differences
  (after the reverts in 21.4).

### 21.4 Tried and reverted

- **Seed translations weighted above co-occurrence guesses** (0.85 against 0.5, both 0.7 before): the Turkish
  question above moved from rank 50 to 22 in `query`, but forge_mod JSON retrieval lost 3 facts (50 -> 47) and
  Verinoda's own Turkish user questions lost 5 in analyze (13 -> 8). Reverted.
- **Turkish vowel narrowing in seed keys** (`engelle` matching `engelliyor`): it also let `yenile` match
  `yeniliyor` and pulled the ranking of those user questions towards update code (analyze 13 -> 8). Reverted;
  the block cue of the Mixin claims reads `engel...` directly.

### 21.5 Not done

Bytecode checks of the target descriptor (item 4's reference tree), `@Shadow` members, Mixins written in
Kotlin, NeoForge access transformers.

## 22. GameTest registry and the tests a change should run (D49, 2026-09-26)

### 22.1 Why

Fabric runs only the GameTest classes a `fabric.mod.json` names under `fabric-gametest` (server) or
`fabric-client-gametest` (client). The impact view listed every test file within four import hops: on a mod
whose main class imports every feature that was 66 test files for a one-file change, without saying which of
them run or which are nearest.

### 22.2 Decisions

- **`verinoda/gametests.py`**: `registry()` reads the entrypoints of every `fabric.mod.json` outside build
  output (a file with junk after its JSON object is still read); `gametest_classes()` groups the `@GameTest`
  methods (`verinoda.testcode`) by class; `for_change(g, seeds)` ranks the classes whose tests reach the
  changed symbols: directly (static test reach, two steps) or through a neighbour class, one whose method calls
  or registers a changed symbol (not a hub over 60 edges in, like the mod's main class): a test that summons an
  NPC, then waits for the tick handler the change is in, reaches the NPC's class, not the handler. Registered
  first, then by distance, then by folder distance from the change. A class that reaches the change but no
  entrypoint names is a warning: it never runs.
- **Where it shows**: `map --view impact` (`gametests` in JSON, "GameTests to run (registered, nearest first):
  ..." in text) and `review` / MCP `change_review` (under Tests).

### 22.3 Measured

On a private Fabric mod (89 GameTest classes, all registered), a change to a guard NPC's file: the three test
classes of that NPC first, then three that reach it through the command class; before, 66 test files in
alphabetical order.

### 22.4 Not done

NeoForge (`@GameTestHolder`, `RegisterGameTestsEvent`), Fabric client game tests that are not `@GameTest`
methods, runtime selection (`runGameTest` with a filter).

## 23. Backlog items and code comments (D50, 2026-09-26)

### 23.1 Why

A project that keeps a numbered backlog (`## 33 - ...` sections, `| 69.3 | **Title** | root cause and fix |`
rows) cites its items in the code: `// why: 12.3 - the lookup by id misses a fresh body in its first ticks`. That
comment is the shortest way from a line to why it is so, and the item is the shortest way from a decision to the
code that carries it. Verinoda read the comment as text and the backlog as a document, with nothing between them.

### 23.2 Decisions

- **`verinoda/backlog.py`**: items are the table rows and numbered headings of the backlog files (`backlog.files`
  in `.verinoda/config.json`; default the first of `docs/BACKLOG.md`, `BACKLOG.md`, ... that exists). A comment
  cites an item with a dotted number anywhere in it, or a whole number as its leading label (`// 33:`,
  `/** why: 33 -`), and only an item the backlog has: `12 ticks` or a string `"12.3"` is not a citation.
- **Lines to items** (`items_for`): the comments on the lines and just above them (a `// why:` sits above its
  code), and the declaration comments of the fields the lines use in the same file: `bodyRef.get()` is
  explained by the `// why: 12.3 - ...` above `bodyRef`.
- **Where it shows**: `verinoda backlog <item | file:LINE[-LINE] | symbol>` (an item: its text and every comment
  that cites it; lines or a symbol: the items that explain them, with the citing comment and the field it came
  through); `query` prints `backlog: 69.3 Title (docs/BACKLOG.md:2235)` under a unit whose comments cite an item;
  MCP `node_inspect` lists them under `backlog`.

### 23.3 Measured

A private mod (548 items, 383 of them cited from 2,444 comment lines): the `WeakReference` line of the guard's
body lookup returns item 69.3, whose row gives the cause ("being loaded is not existing"), through the field it
reads; the method's own javadoc label returns its section, 33.

### 23.4 Not done

Items in other formats (issue trackers, `- [ ] 12.3` task lists), analyze claims from an item (it answers "why"
only through `query` and `node_inspect` for now), items cited from documentation files.

## 24. Java access, constructor types and the classpath as a reference (D51, 2026-09-26)

### 24.1 Why

The Java check (D43) knew a method by its name and number of arguments. `cas.isImmobile()` on a mod's NPC passed:
the method exists, but it is `protected` in `LivingEntity` and the calling class is neither a subclass nor in its
package, so the code does not compile. `new ChunkPos(pos)` with a `BlockPos` passed wherever some one-argument
constructor existed (`ChunkPos(long)` in older versions). And an agent had no way to ask what a game class really
offers: `verinoda api` read Python only, so invented Minecraft APIs were caught only after they were written.

### 24.2 Decisions

- **Class files keep more** (`jvmclass`, index version 3): the public / protected / private bits of methods and
  fields, and each method's parameter types.
- **Access**, for members read from class files (a project source's modifiers are not recorded, so its members are
  never judged): private only from the same top-level class; protected from the same package or a subclass
  (or a class nested in one); no modifier from the same package. A call no overload of which the calling class
  can reach is `absent` with `access` and the reason ("protected in net.minecraft.world.entity.LivingEntity: not
  accessible from app.Outside (not a subclass, another package)").
- **Constructor parameter types**: among the constructors with the right number of arguments, one whose parameter
  a known argument type cannot fill (a class type for a primitive, a class that does not extend the parameter's
  type) does not fit; none fitting is `absent` with the signatures there are ("no constructor of ChunkPos takes
  (BlockPos): they take (int, int)"). An unknown argument type, a boxed primitive, a varargs constructor or a type
  whose super types are not all read always fits.
- **`verinoda api` reads Java** (`codecheck_java.api`): a class by its full name, a simple name (the candidates
  when several classes have it) or `Class.member`, as the build sees it: the project, its classpath (Loom's
  Minecraft jars included) and the JDK; own members first, then inherited ones with where they come from,
  constructors only of the class itself, with access and `static`. It is tried first in a repository with Java
  sources, for a name with a capitalised part; Python follows when it finds nothing.

### 24.3 Measured

- The acceptance lines in a scratch file of a private Fabric mod: `cas.isImmobile()` absent (protected), `new
  ChunkPos(pos)` absent, `new ChunkPos(1, 2)` and `cas.getHealth()` exist.
- The whole mod (407 files, 127,176 sites, a build that compiles): 0 absent, as before.
- `verinoda api net.minecraft.world.level.ChunkPos`: 2 seconds with the jar tables cached.

### 24.4 Not done

Method arguments' types (only constructors are matched by type), the receiver rule of protected access
(`other.protectedMethod()` on a sibling class from a subclass), project sources' modifiers, SCIP symbols as a
reference tree.

## 25. Datapacks: function calls, entity tags and scoreboard objectives (D52, 2026-09-26)

### 25.1 Why

A mod with a datapack keeps a large part of its behaviour in `.mcfunction` files, which the graph did not read at
all (628 files in the private mod measured here). Their calls (`function`, `execute ... run function`, `schedule
function ... 40t`) were invisible to `trace`, `when` and impact, and the state they share with Java - entity tags
and scoreboard objectives - was two unrelated piles of text. The bug this hides: Java checks a tag nothing adds
(the vampire the upkeep tick never found), or mcfunction writes a score nothing reads.

### 25.2 Decisions

- **Functions in the graph** (`project_index/extractors/mcfunction.py`): one node per function file, labelled with
  its id (`ns:path`); `function` and `execute ... run function` are `calls` edges, `schedule function x 40t` a
  `registers` edge with the delay in ticks (`s` and `d` converted), so `when` says "40 ticks later"; a function a
  datapack's `#minecraft:tick` or `#minecraft:load` lists carries it (`metadata.events`), so `when` ends there
  ("every server tick"). For a call, the `execute if/unless ...` part of its line is the condition. A path to the
  file resolves to its function node.
- **`verinoda/datapack.py`**: tags (`tag ... add/remove`, `{Tags:[...]}` in `summon` / `data merge`, `tag=` in
  selectors; Java `addTag` / `removeTag` / `entityTags().contains(...)` and their scoreboard-tag forms, through a
  `static final String` constant when that is the argument, through the project's own helpers that wrap them, and
  commands written as strings) and objectives (`scoreboard objectives add`, `players set/add/operation`, `execute
  if score`, `scores={...}`, `store result score`; in Java, a string naming a known objective, read or written by
  what the called helper does with it). A tag a macro fills in (`$(tag)`) is said, never matched.
- **`verinoda datapack`**: the counts, then tags checked but never added, tags added but never checked, objectives
  written but never read, calls to functions that do not exist; `datapack tag|score|function NAME` for one of
  them, every site with its language and role.

### 25.3 Measured

- The private mod at its current commit: 314 functions (two copies of the datapack), 143 tags, 46 objectives; 12
  tags checked but never added (leads: names built at run time or added outside the mod), 5 objectives written
  but never read.
- The acceptance: at the commit before the fix of a vampire the hunt never found, "tags checked but never added"
  lists both tags of the acceptance: the hunted creature's (checked in Java, added nowhere) and the broken wing's
  (checked in Java, the mcfunction adding it not yet written); at the fixed commit neither is there.
- `when` on a wing function: every server tick, through six functions from `#minecraft:tick`, with the `execute
  if score ...` condition of the last call.
- fastbench on fresh indexes: the first version lost 2 facts on glow_mod and moved 2 private questions (the
  function nodes, named `ns:path`, outranked the Java that runs them as names); with mcfunction files kept
  as data files for search (their nodes still serve when, trace and impact): 0 differences on the three
  sets with datapacks.

### 25.4 Not done

Advancement and predicate JSON (a function run as an advancement reward), `return run`, macros' arguments, Java
running a function by a built string, NBT `Tags` set from Java.

## 26. Stack traces and GameTest results of a log (D53, 2026-09-26)

### 26.1 Why

A GameTest run's log says what happened: a stack trace printed where an NPC was discarded, and the result line of
every test. Read by hand, the trace is sixty frames of Minecraft with two of the mod's in between, and nothing
connects "discarded by `GameTestInfo.succeed`" to the neighbouring test whose `succeed()` cleared its area at that
moment.

### 26.2 Decisions

- **`verinoda trace-log FILE`** (`verinoda/trace_log.py`): every stack trace (an exception, or a bare
  `java.lang.Throwable` after a marker line such as `[GUARDREMOVE] DISCARDED ...`, whose marker becomes the
  trace's title) and every GameTest result line (`... passed`, `... failed`).
- **Frames**: a frame whose class the project has (by its package path) is mapped to the method node (by name and
  the line; a `lambda$m$3` frame to the method around the line), shown with its callers; the frames outside the
  project are folded into one line naming the first, the last and the ones that matter (`GameTestInfo.succeed`,
  `Entity.discard`, a tick).
- **Tests**: a result line is matched to its `@GameTest` method (the id's last parts, case-insensitive). A trace
  through `GameTestInfo.succeed` / `fail` or `GameTestHelper.succeed` is tied to a test: the one whose method is on
  the stack, else the one whose result line is nearest (the same timestamp first).
- **Stored**: one claim per trace tied to a test or to a project method. A log is a run Verinoda did not make, so
  its lines are evidence of the `agent_report` kind (D34): the excerpt with its hash, the log kept under
  `.verinoda/logs/`; such evidence never verifies, and the claim that asks for `observed` gets what the rules
  allow (`weak_inference`). `--no-store` reports only.

### 26.3 Measured

A synthetic log of the acceptance case on a private mod (the original log was not kept): the discard trace is tied
"via GameTestInfo.succeed" to the neighbouring test whose result line has the same timestamp, the mod's frame shown
and six game frames folded into one line.

### 26.4 Not done

Traces interleaved from several threads, `Caused by` chains shown as their own traces, NeoForge's GameTest log
lines, obfuscated (intermediary) frame names.

## 27. Shaders: uniform blocks, their Java writers and mirrored constants (D54, 2026-09-26)

### 27.1 Why

"Where does `Weather.y` come from?" A shader reads it from a `std140` uniform block; Java fills that block with a
run of `putMat4f` / `putVec4` calls in the same order. Only the order links a field to the expression that fills
it, so a field added on one side shifts every later field without an error, and a constant table the shader
mirrors from Java (`#define MAT_STONE 1`, `STONE(1, ...)`) can drift the same way.

### 27.2 Decisions

- **`verinoda/shaders.py`**: the uniform blocks of `.glsl` / `.fsh` / `.vsh` / `.vert` / `.frag` files (field
  types, names, lines), the runs of `put...` calls on one builder in Java, a block paired with the run whose types
  match it in order (exactly, or on the fields before its first array); every field and each `x/y/z/w` of a
  vector then has the Java expression that fills it. Mirrored tables: a shader `#define PREFIX_NAME n` group and a
  Java enum whose constants take the same names with their number first.
- **`verinoda shader NAME`** answers `Field`, `Field.x`, `Block.Field.x` with the expression and its line;
  **`verinoda shader --check`** lists what disagrees (exit 3): a block and a writer that differ in length or type
  from some field on, constants with different values or on one side only.

### 27.3 Measured

A private mod: 4 blocks, 2 writers, the 20-field frame block paired exactly; its wetness component answered with
the Java call that fills it (by `verinoda shader` and by `analyze` for the question as asked), and the 16-entry material table agrees on every value. A one-sided change in the test
fixture (a field added to the block, a constant renumbered) is reported by `--check`.

### 27.4 Not done

Graph nodes for shader functions and `#moj_import` edges, blocks filled in loops or through helpers, `uniform`
variables outside blocks, post-effect JSON.

## 28. English questions over code named in Turkish (D55, 2026-09-26)

### 28.1 Why

A mod written by a Turkish developer names its methods in Turkish (a body lookup, a list of threats) and documents
many of them in English. An English question found almost none of them: on 30 questions about such methods of a
private mod (15 in English, 15 in Turkish, a development set written for this item), 0 of the 15 English ones had
their method in the first three results (MRR 0.023); the Turkish ones 8 (MRR 0.486).

### 28.2 Decisions

- **A symbol's leading comment is its own text** (search index schema 5): the javadoc or `//` block right above a
  Java, Kotlin, C-family, Go, Rust, JS/TS ... method or field was outside the symbol's span, so its words counted
  for the enclosing class. They now go to the symbol's unit, and the class's unit no longer holds them. For a
  Turkish name its javadoc is often the only English there is.
- **The seed dictionary backwards** for an English question: an English word's Turkish seed keys (`order` ->
  `siparis`), tried with the endings a name part carries (`-ler`, `-i`, `-si`, `-de` ...) and kept as the index
  cuts them (the English stemmer makes `govd` of `govde` and `siparisl` of `siparisler`), weight 0.5. Only where it
  can help: a token of the code (a name or a code unit's text, not a glossary or Turkish prose), and only for a word
  the code does not already name things with in English (`angel`, `item`: their Turkish glosses would pull in every
  Turkish name that shares them).
- **Sixteen generic seed words** used by game and tool code (`tehdit` threat, `dusman` enemy, `etraf` / `cevre`
  around, `durak` stop, `konum` position, `ipucu` clue, `menzil` range, `bekci` guard ...).

### 28.3 Measured

- The 30 questions (kept outside the repository with the private corpus): top-3 18 of 30 (was 8), English 8 of 15
  (was 0, MRR 0.023 -> 0.501), Turkish 10 of 15 (was 8, MRR 0.486 -> 0.627). The leading comments alone moved
  English to 6 of 15. The seed words were chosen while looking at this set, so it is a development set.
- "threats around the player" finds the Turkish-named threat list in the first three. "body lookup" does not reach
  its method: about twenty methods of that mod are named after a body, and nothing in the question separates the
  lookup from the others (its method ranks in the thirties).
- fastbench on fresh indexes, old against new: analyze and plain-text retrieval unchanged on all nine sets; the
  JSON retrieval lost 4 facts (a class's unit no longer spans its methods' javadoc words, and a whole-class unit
  had counted every fact of its file).
- Measured and dropped on the way: the comment in both units (weaker on the 30 questions, a fact lost by analyze on
  the private set); the reverse seed at weight 0.7 and on words the code names in English (a fact lost by analyze
  on an English question of the private set); `koru` glossed as guard or protect (a Turkish question about a
  server's protection went to the guards module: 2 facts lost); reverse glosses from prose and data tokens (a
  held-out question lost a fact to the seed glossary file itself).

### 28.4 Not done

Pairs learned from comments next to Turkish names (too sparse here: "threat" is written once near such a name), a
Turkish stemmer in the tokenizer itself (`dusmanlar` -> `dusman` in the index), the Turkish question over English
names beyond the seed.

## 29. An analysis said once (D56, 2026-09-26)

### 29.1 Why

An `analyze` answer is read in full by an agent. Measured over the nine benchmark sets (fastbench now counts the
characters of every answer), the answer itself was about a tenth of the text: the passages 56-73 %, the claims
"found on the way" 10-12 %, critique lines 2-3 %; and with an index older than the working tree the changed files
were listed up to four times (the header, the answer's unknown, the passages' note, again per sub-question).

### 29.2 Decisions

- The changed files are listed once, under the answer's "does the index describe the current working tree?"
  unknown; the CLI header says how many, the passages inside an analysis no longer repeat them.
- A claim's uncertainties are printed without repeats (critique's "call site path:line: <reason>" restates a
  reason the claim already gives).
- At most six context claims after the answer (the rest counted; `--json` has them all); a critique line's
  findings clipped to 140 characters.

### 29.3 Measured

fastbench on the same indexes: analyze facts 318 -> 318, analyze characters -1.6 % (the benchmark's indexes are
fresh, so the stale-file saving does not show there). Tried and dropped: a smaller passage budget when every
sub-question is met (2,400 characters: -15 % characters, -6 facts; 3,600: -9.5 %, -3 facts), and window lines
without their path (the text is budget-bound: the characters saved were filled with more of the same, and the
path was lost).

## 30. Java overloads; answers read as a person would (D57, 2026-09-27)

### 30.1 Why

Reading `analyze` answers over a real Minecraft mod the way a person would showed six faults:

- Java overloads were one node. The extractor mints a method's id from its class and name, so a second
  `moon(server, night, full)` next to `moon(server)` was dropped: its span was the first overload's (a one-line
  delegate), its body's calls were counted as the first one's (a self-call "`moon` calls `moon`"), and the method
  that holds the logic had no passage of its own. The mod had 135 such groups (283 methods).
- "How does the car move each tick?" was answered `met_with_inference` by three paths from the mod's initializer
  to another class's saved data: a flow question with one subject fell back to entry-to-storage paths, and those
  were matched to the question's code by *label* (`.tick()` names a hundred methods).
- "What breaks if I change the signature of `Cls.method`?" got a file-level list (the file possibly affects a
  backlog document, ...) at `weak_inference`, not the method's callers.
- A class whose doc comment sits above its declaration printed empty, inverted windows (`File.java:104-76`):
  D55 made the comment the class's own text, but the text renderer clamped a window to the span's first line.
- A Turkish stem matched a function word (`yanıyor` "is burning" -> `yani` "that is"; `button` -> `but`).
- In the JSON answer, characters freed anywhere went to one more edge between items already shown, and the
  `more` list (the next candidates, often the one fact still missing) no longer fit.

### 30.2 Decisions

- Java: a method whose id is taken by an earlier overload of the same class gets `name_2`, `name_3` (same
  label) and `arity` / `varargs` metadata on every overload. A call binds to the overloads its argument count
  fits (same file, the cross-file member-call resolver, and Verinoda's `java_calls` pass, which counts the
  arguments on the call's line, strings and nesting aware); when the count decides nothing, to all of them as
  INFERRED. The AST cache schema (5) and the receiver sidecar (8) change: run `verinoda scan .` once.
- A name that means several overloads of one class resolves to the group (the first is the node, a note lists
  the lines); `when` merges the groups' paths (not the ones through another overload), `trace` tries every pair.
- A flow sub-question with one subject and no storage word: when no entry-to-storage path passes through its code,
  it is `mechanism` - the subject's calls answer it, at most `met_with_inference`, with an unknown that says
  why. Entry-to-storage paths are matched to the question's code by node id (hops carry `from_id` / `to_id`).
- The repository-wide entry-to-storage view stops at 20 paths; in a large project the code a question is about
  is often on none of them ("where is the car's parking spot saved?" found none of 20). A storage question then
  gets the paths through its own code (`architecture_map.paths_through`): from each of its symbols (a class
  stands for its methods) the nearest write, and back from it the nearest entry point or a caller nothing in the
  project calls (a framework override); "X writes at X" alone is not a path.
- Impact on a symbol the question names (a method): "A change to `Cls.m` reaches the code that calls it:
  `A.x` (file:line), ..." with the call-site lines as evidence, before the file-level view.
- A symbol's leading comment prints at its own lines when a passage of it ranks.
- Query expansions by Turkish stem or corpus prefix never add a function word; `yani cunku ancak bile diye zaten`
  joined the Turkish stopwords, `kir` / `kirik` / `bozuk` the seed dictionary.
- The JSON answer keeps room for the first two `more` candidates before edges between shown items.

### 30.3 Measured

fastbench, fresh indexes built by each code (fb_base57 / fb_new57b): text and analyze facts 315 / 318 unchanged,
JSON retrieve 253 -> 262 (the `more` room: +9, no loss), characters within 0.2 %. The first expansion filter
(every route) lost a fact where a lexicon identifier's part (`not` of `not_a_forge`) mattered: it applies to stems
and prefixes only. The TR/EN ranking set (bench_tr) stays at top-3 18/30.

## 31. Less noise in an answer (D58, 2026-09-27)

### 31.1 Why

Asking Verinoda about its own code with a working tree ahead of the index ("how does the text answer of query
decide which passages fit the character budget?", "which tests cover naming.resolve?") showed:

- four unknowns "what does .github/workflows/release.yml (changed since the index) do with `query`?": the plain
  words "query" and "text" had linked to functions of those names, and every changed file spells them;
- context claims the critique itself had refuted ("`test_...()` reads environment variable X" -> contradicted),
  each with its critique line: the reader learns nothing about the question from them;
- "how does X decide ..." read as a configuration question ("what decides / controls X"), answered with
  environment variables;
- `naming.resolve` not linked by its name ("no symbol in the index is named `naming.resolve`"), although
  `verinoda trace` resolves it;
- a claim "`naming.py:210-212` contains: " with nothing quoted (the lines had moved in a changed file).
- a why-question took 100 s and ran out of its 60 s budget: the history view searched the whole design document
  once per symbol of the graph (30,000 regular-expression searches over 3,300 lines);
- the decision record a why-answer cited was quoted by its first line (the introduction), not where it gives
  the reason.

### 31.2 Decisions

- The stale-file guard keeps a name the question linked only when it looks like an identifier (an underscore, a
  digit or an inner capital) or is a carried subject; a name written as code keeps its own path.
- A contradicted claim that is not an answer is counted after the context ("+N context claim(s) the critique
  refuted"), not printed, and neither is its critique line; `--json` has them.
- "how does/do/is ... decide/determine/control" drops the config cue when the clause is also a flow question.
- The linker matches `module.function` and `package.module.function` to the function of the file that module
  names ("qualified", a name tier).
- A module-block claim with no text to quote is not made.
- The history view reads each decision document's words once and looks the symbol names up in that set:
  90 s -> 0.9 s on Verinoda's own repository, the same decisions and mentions.
- A why-answer quotes and cites the line of the decision document that carries the most of the question's
  topic words and matched names (names count double), inside the heading section with the most of them.

### 31.3 Measured

fastbench on the same indexes: analyze facts 318 -> 317, characters -0.3 %; the fact lost matched only a token
inside a refuted claim about a test file (the fact's own lines are elsewhere): a scorer artifact, kept as measured.

## 32. Settings read by a string key (D59, 2026-09-27)

### 32.1 Why

"Which config key sets how long the car door stays open?" over a Minecraft mod came back `unmet` ("no env reads
matched"): the config view knows environment variables, and a mod (like most JVM and many web projects) reads
its settings through its own object by a dotted key, `Settings.number("car.door-wait-ticks", 140)`, backed by a YAML
file.

### 32.2 Decisions

- `architecture_map.config_key_reads`: calls whose first argument is a string of two or more dotted parts, the
  second argument taken as the default; calls that take such a string for another reason are left out by name
  (`translatable`, `id`, `format`, logging, `equals`, ...).
- `architecture_map.config_key_definitions`: dotted keys defined by the repository's YAML (nesting followed),
  TOML (`[section]`) and flat `key = value` files, with their line.
- The config handler reads those calls in the eight files that ranked highest for the question, scores each key
  by the question's words among its parts (an English word also by the Turkish words the seed dictionary glosses
  it with: "door" finds `kapi`), keeps the best-scoring keys (at most three), and claims "`tick` reads setting
  `car.door-wait-ticks` (default 140) (file:line); ... is set in settings.yml:12" as `strong_inference` with both
  lines as evidence (a pattern found it; which object answers the read is not traced).

### 32.3 Measured

fastbench: facts unchanged, analyze characters +0.2 % (the new claims). On the mod: the key the question asked
about first, then only keys with as many of its words.

## 33. What an agent carries and cites (D60, 2026-09-27)

### 33.1 Why

An agent-in-the-loop pilot (a model answering questions about a small repository with only file tools, with
Verinoda, and with Graphify; one run per tool and task profile) showed three costs that are Verinoda's, not
the task's:

- The agent cited the span of a whole function (14 lines) where one or two lines held the statement: the
  passages named their span in the header and printed the lines unnumbered, so the header was the only locator
  it had. A citation rule of the task (at most 8 lines per range) then failed the answer's evidence, and a reader
  gets a function to search instead of a line to read.
- Every request carried the tool menu and the server instructions (12,496 and 2,151 characters, about 3,700
  tokens) - over about fifty turns, a large share of the session's input - including decision_check in a project
  without decision records, and instructions that repeated the tool descriptions.
- The agent ran code_check on a read-only question (11,600 characters of results), and its first call,
  index_update, came back not_initialised: the folder had not been scanned, and the agent had to find the shell
  command.

### 33.2 Decisions

- `retrieval.render_text` prints each passage line with its line number, right-aligned per window; blank lines
  are left out (the numbers keep every line's place). The `## path:a-b` and `  path:x-y` locators are unchanged.
  node_inspect's excerpt is numbered the same way (`retrieval.numbered_lines`).
- Tool descriptions of the core profile say what the tool returns and its limits once; parameter descriptions
  lost what the tool description or the result already says. The instructions name each core tool in the order
  of use and keep the rules; the claim-status list is in every result that has a status.
- `served_tools`: the core profile lists decision_check only when the project's decisions folder holds a Markdown
  file (a folder that cannot be read keeps it listed, so the tool can say what is wrong); the instructions then
  leave out its sentence. `--profile full` always lists it. The menu is read at startup: records added later show
  after a restart.
- `index_update` on a folder with no `.verinoda/` runs the first scan (`verinoda scan`), except in the user's home
  folder, a drive root or a workspace of two or more projects (sub-folders with their own `.git` or `.verinoda`):
  a wrongly resolved project, not_initialised with the `--repo` hint. Every other tool
  still refuses with not_initialised and now names index_update. The server creates `.verinoda/` only then.
- The skills and the instructions: cite the narrowest lines that hold a statement, not a function's span;
  code_check is for code the agent writes or edits.

### 33.3 Measured

- Menu and instructions as Claude Code receives them: 14,647 -> 10,521 characters without decision records
  (-28 %; 11 tools), 11,539 with them (-21 %; 12 tools).
- fastbench, nine sets, 333 question x approach cells: one fact lost (heldout h08, analyze and query text: the
  last passage no longer fits the 6,000-character budget). Characters per set -2.2 % to +3.0 % for query text
  (+3.0 % on orders_app and orders_app_tr, +2.4 % on graphify_core) and -1.7 % to +1.6 % for analyze; the sets
  whose answers fill the budget stay within 1 %.
- The pilot's effect on the agent is measured again on the next frozen build, on tasks other than the pilot's.

## 34. A four-tool menu and a gateway (D61, 2026-09-27)

### 34.1 Why

A size-threshold study (28 paired question sessions over repositories of 139 to 760,000 code lines, a model with
file tools against the same model with Verinoda) found no repository size below which Verinoda should step back:
the median paired token difference was -14 % on the smallest. What it did find: the Verinoda session's first
turn was 3,933 tokens larger in every pair (the tool menu, the server instructions and the skill's description),
the skill itself was opened in none, and where the model did not call Verinoda at all the session cost more
(median +19 %). Over 51 benchmark sessions the model called analyze, project_query, index_update, node_inspect,
code_check and relation_trace, and never map_view, the claim tools, change_review or decision_check. Those
sessions were all question-answering: "never called" is shown for them, "never needed" is not - change_review
and decision_check belong to editing, which the sessions did not do.

### 34.2 Decisions

- `CORE_DIRECT`: the core menu lists project_query, analyze, code_check and index_update. analyze takes the
  question and a time budget, code_check paths, a diff, or a snippet with its path; plans, test runs, tracing,
  call budgets and environments stay in the full profile and the CLI.
- `run_tool {name, arguments}` reaches every other core tool: node_inspect, relation_trace, map_view,
  claim_list, claim_inspect, evidence_inspect, change_review, and decision_check in a project with decision
  records. Its description names each with its arguments; a call is checked against the tool's own signature
  (pydantic `validate_call`), and a wrong one comes back as `invalid_arguments` with what was wrong and the
  tool's parameters. Not read-only (change_review can run tests).
- The instructions name the four tools, then what run_tool reaches, and keep the rules of D60 (narrowest lines,
  code_check on code written).
- `--profile full` is unchanged: every tool listed with every argument.

### 34.3 Measured

- Menu as listed: 9,385 -> 4,253 characters (5 tools; 11 before).
- First turn of a Claude Code session (the same orders_app question with and without Verinoda, a build of this
  code): +3,933 tokens before, +2,177 now (-45 %). Expected before measuring: 1,800-2,000.
- Tests: run_tool reaches node_inspect and reports a wrong argument with the tool's parameters.

## 35. Verinoda in the Grep the agent already runs (D62, 2026-09-27; under study)

### 35.1 Why

In every benchmark so far the agent reached for Grep and Read first and often never came back to Verinoda: 15 bug-fix
sessions made no Verinoda call, and on a Minecraft mod the agent grepped its way to half an answer that analyze gives
whole. Two ways to meet it: ask for analyze first on the questions it is for (a), or put what Verinoda knows where the
agent already looks (b).

### 35.2 Decisions

- (b) `grep_context(pattern)`: identifier-like words of the Grep pattern (at least 4 characters, regex and language
  keywords left out), longest first; for the first two the graph resolves by their exact name, one line each:
  where it is defined, up to 3 callers and 4 callees with file:line, counts beyond that. Returned as Claude Code's
  PostToolUse hook JSON (`additionalContext`), cut between entries at 450 characters; `{}` for anything else,
  errors included, so a Grep is never held up. Warm, one call takes about 0.1 s (the MCP server keeps the graph).
- The hook is an `mcp_tool` hook on the Grep tool that calls run_tool with `grep_context` and the Grep's pattern
  (`agents/templates/claude_hooks.json`): no process per Grep. It covers the Grep tool only: in the threshold
  study's sessions 111 of 133 searches went through it, 22 through grep in Bash.
- (a) `ANALYZE_FIRST`: "For a how, why, what-happens or flow question, call analyze once before searching by hand;
  a question that names one symbol or file can start with Grep." Not in the instructions yet.
- Neither is switched on: an adoption study measures both (with a 2 x 2 design) on the build that holds them, and
  each goes in only if it pays.

### 35.3 Measured (adoption study, 2026-09-28)

25 questions not used before (six repositories, 139 to 134,000 code lines), five arms per question started together:
no Verinoda, Verinoda as shipped, + (a), + (b), + both; 125 sessions, a model in the loop. The rule was written before
the first session: switch one on if its arms find at least as many facts as their pairs and the median paired cost
rises at most 10 %.

- (a): facts 140 -> 136 over 50 pairs, median paired cost -1 %. (b): 143 -> 133, +1 %. Neither is switched on. Most
  of both drops is the cell with both (63 of 83 facts, against 70-73 in the others), inside the noise of 25 pairs.
- Adoption: sessions that called Verinoda 14 of 25 as shipped, 15 with (a), 18 with (b), 17 with both. The hook
  fired 41 times in the (b) arm, 22 of them with context, about 65 tokens per session.
- Verinoda as shipped (D61) against no Verinoda on the same questions: facts 65 -> 70, total cost -19 %.

## 36. Ranked unknowns, declared types (D64, 2026-09-28)

### 36.1 Why

A study of the name check's `unknown` verdicts (the diff of the last 20 commits of this repository, 568 sites)
found 125 correct sites and 37 planted misspellings in one undifferentiated `unknown` list, sorted by path. MCP
`code_check` cuts its answer at 12,000 characters, and a listed site averaged 610 characters: the first answer
kept the 12-13 absent sites and none of the 37 planted unknowns. Most unknowns were correct code whose receiver
type jedi did not infer; two jedi 0.20 defects accounted for many of them (a comprehension variable used in the
comprehension's `if` clause infers to nothing; `Path / "x"` infers to `PurePath`, so `mkdir`, `write_text` and
`exists` are not found), and pytest's own fixtures (`tmp_path`, `monkeypatch`) had no type at all. Two kinds of
false `absent` were also seen: an import inside `with raises(ImportError)` that the test expects to fail, and an
optional dependency listed only in a requirements file under `tests/`.

### 36.2 Decisions

- **A rank for every unknown Python site** (`rank`, `rank_why`, `codecheck_rank.py`); the verdict never changes.
  - HIGH: the name is defined nowhere in a word index of the project, its environment's Python sources and
    jedi's stubs; or the receiver's declared type lacks it and has a close name (edit similarity >= 0.8).
  - MEDIUM: the declared type lacks it (only a subclass or runtime code could add it); a `**kwargs` callee would
    take a name defined nowhere; a keyword defined nowhere that the checked code reads back as an attribute
    (`SimpleNamespace(retry_ms=3)` ... `cfg.retry_ms`); the receiver is bound by an import that is not installed,
    or is a local bound from such a receiver in the same function (`df = pd.read_csv(p)`); an import that was not
    decided; a name not found in an index that stopped early; an absent name that is the whole body of a
    `with raises(E)` block (below).
  - LOW: the receiver's type is not known and the name is defined somewhere.
- **The word index is a superset test.** Every identifier-like word in the text of site-packages, the standard
  library and jedi's typeshed counts as defined (comments and docstrings included), plus option strings as
  argparse turns them into names (`"--no-mcp"` -> `no_mcp`) and the running interpreter's built-in names. The
  project's other files count with their words; the checked files only with the names they define (definitions,
  parameters, stores, imports, string constants; for an attribute site also the keywords they pass), so the
  misspelling itself never counts. Names that only a compiled extension defines are outside the index; a
  `__getattr__` on the receiver's container keeps a site out of HIGH. The environment's index is built once per
  environment fingerprint (and jedi version), kept in memory and in the user cache
  (`%LOCALAPPDATA%/verinoda/Cache/names/names-<fingerprint>.txt`, `~/.cache/verinoda/...`, or
  `$VERINODA_CACHE_DIR`; one JSON header line, then one word per line), whether or not the project has
  `.verinoda/`; the fingerprint hashes the interpreter's absolute path, so projects that share an environment share
  its index. One check spends at most 120 s on it (`VERINODA_NAME_INDEX_BUDGET_S`; a value that is not a number
  falls back to 120 with a warning), and with a time budget (MCP `code_check`) at most what the budget left. A
  build cut short is kept with the number of files it read (the walk is sorted, so the order is fixed) and the next
  check continues it; until it is complete a name not found in it is MEDIUM, never HIGH, and the note names the
  budget that stopped it. `--no-cache` does not touch it (it is an index of the environment, not of the checked
  files; deleting the file rebuilds it).
  The receivers bound by an import that is not installed are read from each checked file's own text (a snippet's
  text; in a diff the whole file, so an unchanged import line counts): an import line that has a site keeps its
  verdict, one without a site (a diff's unchanged lines) is looked up in the environment's search path.
  Precision limit (not measured): without a project environment (`--env none`, no `.venv`) the index holds only
  the standard library, the stubs and the project, so a name of an uninstalled third-party package reached
  through another module (a function that returns a DataFrame) can be ranked HIGH.
- **Order and listing.** Sites are listed absent, HIGH, not installed, MEDIUM, guarded, then LOW. LOW sites are
  counted by cause in `unknown_summary` (`high`, `medium`, `low`, `low_by_cause` with an example and one next
  step per cause) and listed only with `--all` / `include_exists`. The ranking runs after the per-file cache, since
  a rank depends on what every other file defines. Java, Kotlin and TypeScript sites are not ranked and stay
  listed (`unknown_summary.not_ranked` counts them, so the counts sum to `summary.unknown`). Every unknown carries a one-step `next_step` (the cause's step when the site had none).
- **MCP one-line sites.** `code_check` returns each site as one line: `VERDICT path:line:col kind expr | why |
  guard: ... | swallowed by ... | optional dependency | elsewhere: qualname (at) | nearest: names` (or `next:
  step` when there are no nearest names; each part only when the site has it); a long `why` is cut in the middle,
  so its tail (where the name was looked for, a swallowing handler) stays; HIGH, MEDIUM and LOW label unknowns;
  the `files` list keeps only files that could not be read. The cap cuts from the end, so an absent or HIGH site
  is never cut before a LOW one.
- **Declared types decide `exists`, never `absent` (the D32 asymmetry).** When jedi's goto finds nothing, the
  receiver's declared type is read: jedi's inference of the receiver; a comprehension variable at its `for`
  target; a local bound once to a path join, and `a / b` itself, from the left operand's pathlib class; a call
  from the called function's declared return type (jedi `execute`); an unannotated parameter of a `test*`
  function or a fixture in a pytest file, named like one of pytest's own fixtures, from that fixture's class
  (unless the project defines a fixture of the same name). Any other unannotated parameter (and an expression
  that starts from one) has no declared type: jedi would infer it from the call sites it finds, and one caller's
  class is not the parameter's type (`self` / `cls`, `*args` / `**kwargs`, and pytest tests and fixtures excepted). A name in that type
  is `exists`. A name it lacks stays
  `unknown` and carries `declared` and `nearest` only when the class is closed in itself (then only a subclass
  can add it); a keyword outside the declared method's signature is `unknown` with the same fields.
- **False absents.** An import inside `with raises(E)` (`pytest.raises`, `assertRaises`, sympy's `raises`)
  whose E is `ImportError` (or a broad `Exception`) is `guarded`. An absent attribute, keyword or dict key is never
  guarded by such a block: a misspelling there raises the expected `AttributeError` / `TypeError` / `KeyError`
  before the error the test means, so the test passes for the wrong reason. Only when the site is exactly the one
  statement of the block (`with raises(AttributeError): obj.gone`, a test that the name is missing) is it
  `unknown` MEDIUM (`expected_error`) instead of `absent`. A module whose package a requirements file anywhere in
  the project lists by exactly its name (normalized; `tests/requirements/postgres.txt`, a bare name counts) is
  `not_installed` with `optional: true`, not `absent`. Those files belong to docs, examples and sub-projects too,
  so a module that only resembles a listed package (`sentry` for `sentry-sdk`) stays `absent`, with the listed
  name as a hint in its message; a file with a line that is not a requirement (a README in `requirements/`) is
  not read.
- Cached answers of the previous rule set are not reused (`CHECK_VERSION` 6).

### 36.3 Measured

`verinoda check --json --all --no-cache --diff HEAD~20` on two clones of this repository at dd60358 with 50 names
planted on changed lines (30 attributes, 20 keywords), the worktree's `.venv` as the environment; 568 sites
each. The planting and triage rules were written by the rule author (in-sample).

| | planted: absent / HIGH / MEDIUM / LOW | real code: absent / HIGH / MEDIUM / LOW | real unknowns | planted in the first MCP answer |
|---|---|---|---|---|
| misspellings (two middle letters swapped), before | 13 / - / 37 unranked | 0 / - / 125 unranked | 125 | 12 of 50 |
| misspellings, after | 13 / 30 / 7 / 0 | 0 / 0 / 4 / 66 | 70 | 42 of 50 |
| invented names (`charset=`, `make_dir`, `tokenize`), before | 14 / - / 36 unranked | 0 / - / 131 unranked | 131 | 12 of 50 |
| invented names, after | 14 / 12 / 10 / 14 | 0 / 0 / 3 / 64 | 67 | 36 of 50 |

- `exists` on the same 568 sites: 393 -> 448 (misspelling clone), 387 -> 451 (invented clone), from declared types.
- The 7 planted MEDIUM misspellings are `Field(descirption=)` (pydantic's `Field` takes `**extra`) and two
  `rpeo` on declared types whose close names were not close enough; the 14 LOW invented names are names defined
  elsewhere on receivers whose type is still not known (`m.group(2).trim`, `Field(descriptions=)`,
  `info.update(commits=)`, which is valid code).
- Time on a loaded machine (four builders): 105 s -> 130 s and 79 s -> 123 s for the whole check; the word index
  of the environment (16,660 files) took 20-41 s of that and is paid once per environment when the project has
  `.verinoda/` (the clones had none, so every run built it). jedi time fell from 47-55 s to 19-25 s.
- False absents: on a copy of a Django checkout with its `.venv`, `django/contrib/postgres/signals.py` has 1
  `not_installed (optional)` import (psycopg, listed in `tests/requirements/postgres.txt`) instead of an absent;
  the two `psycopg2` imports stay absent (psycopg2 is listed in no requirements file; they sit under
  `if is_psycopg3:`, a flag imported from a module that sets it in try/except ImportError, which is not read as a
  guard). On a copy of a sympy checkout, `sympy/core/tests/test_numbers.py:1431` (`from sympy import Pi` inside
  `with raises(ImportError):`) is `guarded` instead of `absent`; the file has no absent site left.
- Tests: `tests/test_codecheck_rank.py` (ranks, summary, order, the name index's superset rule, argparse dests,
  not-installed receivers, raises guards, optional dependencies, declared types for the two jedi defects, path-join
  locals, call results and pytest fixtures, MCP one-line sites under the cap, CLI labels; and one test per
  finding of the adversarial review: resembling requirement names, prose requirement files, diff and snippet
  ranks, locals from a missing module, duck-typed parameters, keyword-defined attributes, MCP line fields, raises
  blocks around attribute and keyword typos, the resumed index and its note, a bad budget value).
- After the review's fixes, the real CLI (`python -m verinoda check verinoda/paths.py --repo <a copy of this
  repository, 720 .py, no .verinoda/> --env <the worktree's .venv> --no-cache`), on a machine loaded by other
  runs: the first run 44 s, of which 11 s built the environment's index (16,660 files, kept in the user cache);
  then 2.3-4.2 s in most runs (26 s and 38 s once each, load), against 1.6-21 s for dd60358 in the same
  alternating runs. The project's own words (720 files) take 0.7-1.2 s per call.

### 36.4 Not done

- `**kwargs` following (the study's M2), mypy or pyright as a second resolver, the opt-in runtime probes.
- A flag imported from another module (`is_psycopg3`) as an import guard.
- The project's own words are read again on every call (0.7-1.2 s for 720 files); a cache keyed by file stat
  would save that.
- `references/local.py` (`local_versions`, which `declared()` reads) still takes a prose line of a root
  `requirements/*.txt` as a package; only the requirements files read for optional dependencies skip prose.
- The close-name threshold (0.8) leaves a transposed four-letter name (`rpeo` for `repo`, 0.75) at MEDIUM.
- The before/after counts on the study's 6-module, Django 30-file and sympy 30-file samples were not re-run.

## 37. A graph with fewer false calls, and the tests of more ecosystems (D65, 2026-09-28)

### 37.1 Why

Measuring the graph on eight public repositories (Go, Rust, C#, Ruby, PHP, C, C++, TypeScript) and on a large
Python web framework showed calls edges that do not exist, labelled `EXTRACTED`, and noise that crowds out
the code a question is about:

- A member call bound to whatever function of the same name its file defines. Python `super().__delattr__()`
  inside `__delattr__` became a self-loop (828 of the framework's 944 self-loop calls edges, 825 of them
  `EXTRACTED`), or an edge to another class's `__delattr__` of the file. Go `b.Bind(...)` (a parameter of an
  interface type from another package) became `Context.ShouldBindWith -> Context.Bind`, and with it a false
  cycle. Rust `builder.build_parallel().run()` bound to the file's free `run`, `Command::new()` to the file's
  own `new`; PHP `$this->middlewareDispatcher->handle()` became `App::handle -> App::handle`. analyze printed
  these as context (`called by: __delattr__ (...:111); __delattr__ (...:291)`, both false).
- Vendored, minified and generated code was extracted like product code: three minified chart libraries were
  31 % of the nodes and 69 % of the calls edges of a Ruby job-queue repository; flex/bison output added 188
  call edges to a C repository.
- A C# test project (`src/UnitTests/`, `src/IntegrationTests/`, `*.Tests/`) was product code to
  `testcode.is_test_file`: 365 files of a 591-file C# repository, whose tests then led a ranking where the
  product file belonged (study question A1). PHPUnit `FooTest.php` outside `tests/`, GoogleTest `x_test.cc`,
  XCTest, Dart and Elixir tests were missed the same way.
- The C# type-reference pass searched every node for each unresolved reference: 44 % of C# extraction in a
  profile.
- `.hh .hxx .ipp .inl .tpp` were in neither the detection nor the extractor table.

### 37.2 Decisions

- **Member calls bind in the file only through the method's own receiver** (`project_index/extractors/
  engine.py`, `go.py`, `rust.py`; local changes to the vendored extractor, docs/UPSTREAM.md):
  - Python `super().m()` binds to `m` of the enclosing class's bases that the file defines, in C3 order (Python's
    MRO: `class D(B, C)` with `B(A)`, `C(A)` searches B, C, A); `object` is passed over; a base the file does not
    define ends the search, and bases that admit no linearization bind nothing. Otherwise the call stays in
    `raw_calls`, where no pass binds it by name. `self.m()` / `cls.m()` take the own class's `m`, then an in-file
    base's, and only then the file-wide name as before.
  - JS/TS and Ruby: a member call binds in the file on `this` / `self` / Ruby `self.class` (the own class first).
    Ruby: a constant receiver naming a class the file defines with that method (`Foo.make`) binds to it; any
    other Ruby receiver (`capsule.fetcher.x`, a block parameter) is deferred to the receiver-typed resolver
    (`x = Foo.new`). JS/TS defer only `super.m()`; other JS/TS receivers keep the file-wide name as before.
  - PHP: `$this->m()` binds to the own class. A receiver whose class the file states binds to that class's method
    (a typed parameter `Foo $x`, `$x = new Foo()` in the method, a typed or promoted property, whose declared type
    holds whatever is assigned, or `$this->p = new Foo()` in the class; a class of another file binds nothing in the file). An untyped receiver binds only to
    the one same-named method of another class of the file, never to the caller's own class's or an in-file
    base's (`$this->middlewareDispatcher->handle()` inside `App::handle`). There is no cross-file PHP
    receiver-typed resolver.
  - Go: a selector call binds to a method of the receiver's type, where the receiver is the method's own
    receiver, a parameter declared with a type of the package (`func record(h *metricHistory)`), a local or
    package variable of `&T{}`, `T{}`, `var x T` or a file function returning `T`/`*T` (`srv := NewServer()`),
    or a field of such a receiver (`s.h.Serve()`); a method promoted from an embedded struct is found (the
    shallowest depth, one candidate; none when an embedded type of another package comes first). Every name a
    body declares (a range variable, a closure parameter ...) shadows a package variable. A name given
    two types in one scope, a chained call result or a parameter of another package's type stays in
    `raw_calls`. A bare `f()` never binds to a method.
  - Rust: `self.m()` binds to the impl type's (or trait's) own `m`, else the existing `rust_self_type` path;
    `Self::m()` / `Type::m()` / `Type::<T>::m()` bind only when the file has an impl of that type with `m`; a
    bare `f()` or `module::f()` never binds to a method; any other receiver stays in `raw_calls`.
  - `cache._AST_CACHE_SCHEMA` 7: per-file results of the old rules are not reused.
- **No call leaves vendored, minified or generated code** (`project_index/vendored.py`, `vendored_reason`, applied
  in `extract.py`):
  - vendored: a folder `_vendor/`, `third_party/`, `third-party/` or `thirdparty/` anywhere below the scan root
    (exact spelling); `vendor/` at the root, beside the manifest of a tool that vendors into it (`go.mod`,
    `composer.json`, `Gemfile`, `Cargo.toml`) or under a static-asset folder (`static/`, `assets/`, `public/`,
    `wwwroot/`); `deps/` at the root beside `mix.exs` / `rebar.config`; a git submodule (`.gitmodules`) under
    `deps/` or `extern/`. A product namespace `Vendor/`, `Vendors/`, `app/controllers/vendor/`, `lib/deps/` or
    `src/extern/` is product code. The folders are read from the path as the scan gives it, relative to the
    root: a folder above the root, or the target of a junction or symlink, never counts.
  - minified: a `.min.js` / `.bundle.js` name (a `make-bundle.js` script is not one), or a `.js`/`.mjs`/`.cjs`/
    `.css` file (at least 4 KiB) whose first 64 KiB hold at least 90 % of their bytes in lines of code over
    1,000 bytes (a long line of data, such as a lookup table, is not code; `.json` is never minified).
  - generated: a generator's header among the first 40 lines. The marker opens a comment ("Code generated ...
    DO NOT EDIT", `@generated` as a word, "This file is @generated", "A Bison parser, made by", "A lexical
    scanner generated by flex", "Generated by the protocol buffer compiler"), or the comment opens with "This
    file is auto-generated" / "Auto-generated by" and the head warns against editing it ("do not edit",
    "regenerate", "will be lost"). A comment that only mentions generated code, and a marker written in a
    string, are product code. `guards.is_generated` uses the same rule.

  A minified file keeps its file node only (its names are machine names nobody imports). A vendored or generated
  file keeps its definitions, each marked `vendored: <reason>`, so an import of them still binds to them and not
  to a same-named product function (`from vendor.yamlish import parse`), but no edge other than its structure
  (`contains`, `method`, `defines`, `inherits` ...) leaves it and its raw calls are dropped. The clusters, the map and the
  callers views still show those definitions. The build prints which files were reduced. The search index still
  reads the files' text. `"index": {"vendored": true}` in `.verinoda/config.json` (or `VERINODA_GRAPH_VENDORED=1`)
  keeps everything; the value is part of the extraction stamp, so changing it rebuilds the graph on the next
  `update`.
- **Test files** (`testcode.TEST_FILE_RE`): a .NET test project folder (`Foo.Tests/`, `AutoMapper.UnitTests/`,
  `Foo.Test/`, `UnitTests/`, `IntegrationTests/`, `FunctionalTests/`, `AcceptanceTests/`, `UITests/`,
  `E2ETests/`, `Tests/`), an Xcode test target at the root (`MyAppTests/`), `unit_tests/`,
  `integration-tests/` ...; XCTest `FooTests.swift`; `x_test.cc`, `x_unittest.cpp` (also `.c`, `.cxx`; not a
  product's `self_test.c`); `x_test.dart`, `x_test.exs`. `Contests/`, `Latest.php`, `attest.cc`, a folder that
  merely ends in `Tests` (`src/HealthTests/`, `Features/ABTests/`), `app/Models/LabTest.php` and
  `SpeedTest.swift` are not tests (PHPUnit tests live in `tests/`).
- **C# placeholders by label**: a dict label -> first placeholder, built once and updated on each new stub,
  replaces the scan; the result is the same by construction and by test.
- **C++ suffixes**: `.hh .hxx .ipp .inl .tpp` are C++ in detection, extraction, the C++ member-call resolver,
  spans, anchors, search and the lexicon.

### 37.3 Measured

Graph built with `index.build(force=True)` on fresh copies, base = dd60358, with and without each change. These
measurements predate the fixes of the review of D65 (the narrower vendored folders and generated header, the
minified content rule, vendored definitions kept, PHP and Go receiver types, `self.class`, C3 order, the
stricter test folders); they were not repeated after them.

- **C# placeholders** (591-file C# repository, `extract()` of its 513 `.cs` files, twice each): the pass
  4.15 / 4.27 s -> 0.05 / 0.05 s, `extract()` 15.7 / 15.5 s -> 12.8 / 10.7 s (loaded box). Full graph: 15,139
  nodes, 30,677 edges before and after, 0 nodes and 0 edges different.
- **Member calls** (calls edges whose call site is in a file of the language, before -> after, with the
  vendored switch on so #4 does not mix in; "retargeted" = the same caller and line still has a calls edge, to
  another target; hand-checked random samples of the purely removed edges):

  | repository | calls | removed | retargeted | added | purely removed: sample read by hand |
  |---|---|---|---|---|---|
  | Python web framework (2,888 .py) | 22,844 -> 21,805 | 1,944 | 907 | 905 | 1,037, all at a `super()` call (1,017 `super().m()`, 20 `super(C, x).m()`): the caller itself or another class's `m`; none at `self.`. 483 of the added edges are `super()` calls bound to an in-file base; self-loop calls edges 944 -> 118 |
  | Go web framework (130 files) | 1,346 -> 1,267 | 85 | 8 | 6 | 77; 20 read: 4 true (untyped locals `pairs`, `engine`, `msg`, a test recorder), 1 interface method, 15 false (`c.Request.Context()`, `mw.Close()`, `b.Bind()` ...) |
  | Rust search tool (237 files) | 3,171 -> 1,694 | 1,809 | 636 | 332 | 1,173; 25 read: 3 true (a field and locals of in-file types), 22 false (`Command::new`, `PathBuf::from`, `path.parent()`, builder chains on external types) |
  | PHP micro-framework (145 files) | 551 -> 526 | 25 | 3 | 0 | 22, all `$this->x->m()` delegations bound to the caller's own method: false; self-loops 23 -> 0 |
  | Ruby job queue (.rb of 347 files) | 588 -> 458 | 133 | 11 | 3 | 122; 18 read: 5 true (`tab.quiet!`, `result.job_results[k].add_metric` ...), 13 false (`Array#each` -> `ProfileSet.each`, `config.handle_exception` -> the caller ...) |
  | TS web framework (.ts of 481 files) | 746 -> 743 | 6 | 4 | 3 | 2 (`super.route()` self-loop, a `v.toString()`) |
  | C (432 files), C++ (145 files) | 2,775 -> 2,775; 2,408 -> 2,408 | 0 | | 0 | unchanged |

  Added edges, sampled the same way: 15 of the 483 `super()` edges in the Python framework, 14 right (the base
  that defines the method, past in-file mixins that do not); the 15th was `super(override_settings,
  self).__init__()` in a subclass, bound to `override_settings.__init__` - `super(C, obj)` now searches the bases
  of C (after the measurement; the test covers it). 10 of Rust's 332 added edges: all right (`FormatBuilder::new()`
  to `FormatBuilder.new`, not the file's other `new`s; `self.is_empty()` to the own type). A base written as
  `module.Class` gives no `inherits` edge, so the search passes over it (right in the one case seen,
  `socketserver.ThreadingMixIn` defines no `__init__`, but not in general).

  A first version also deferred every JS/TS receiver other than `this`; on the TS framework it removed 37
  edges of which a sample of 12 had 5-6 true (untyped locals of classes the file defines, a typed parameter the
  TS resolver did not bind), so JS/TS defer `super.m()` only.

  The false edges of the study's probe table are gone (`ShouldBindWith -> Context.Bind` and its cycle,
  `files_parallel -> run`, `App::handle -> App::handle`, the `super().__delattr__` self-loop and the
  cross-class `LazySettings.__delattr__ -> UserSettingsHolder.__delattr__`). The price: true same-file edges
  through an untyped local (`engine := New(); engine.With()`, `let p = ...; p.add_child()`, a Ruby block
  parameter) are gone too, about one in five of the purely removed Go edges, one in eight in Rust and one in
  four in Ruby by the samples; typing Go locals from constructors and Rust `let x = Type::new()` would bring
  them back. The graph does not report them as unknown yet: analyze's callers answer says "no call edge".
- **Vendored / minified / generated** (graph before -> after, the member-call change in both):

  | repository | nodes | edges | calls | files reduced to a node |
  |---|---|---|---|---|
  | Ruby job queue | 2,851 -> 1,981 | 4,468 -> 2,413 | 1,628 -> 507 | 3 minified chart libraries, a generated `db/schema.rb` |
  | C JSON processor | 1,353 -> 903 | 5,465 -> 3,864 | | `vendor/decNumber/` (31 files), flex/bison `lexer.c/h`, `parser.c/h` |
  | Python web framework | 47,392 -> 46,937 | 102,969 -> 102,186 | 22,127 -> 21,846 | 68 files under `admin/static/.../vendor/` (jQuery, select2, XRegExp) |
  | Go web framework | 2,017 -> 1,980 | 4,761 -> 4,710 | 1,267 -> 1,260 | a protoc-generated `test.pb.go` |
  | Rust, PHP, TS, C++ | unchanged | | | none |
- **Test files** recognised by `is_test_file`: C# repository 61 -> 421 of 514 code files (+360: `src/UnitTests`
  325, `src/IntegrationTests` 37, `src/AutoMapper.DI.Tests` 3); the C repository +1 (`src/jq_test.c`, its test
  runner); the other seven repositories unchanged.

## 38. Faster scans: the file list from git, one parse, one commit, a renamed search build (D66, 2026-09-28)

### 38.1 Why

A profile of a full scan of a django checkout (6,653 tracked files, 2,888 Python) put the time outside the
tree-sitter pool: detect() spent about 18 s (profiled) evaluating every .gitignore rule per path in Python,
taking a realpath per file and asking git for the tracked files anyway, and it ran again on every update
(6 s of a one-edit update). Python files were parsed 9,209 times for 2,888 files: the receiver sidecar parsed
1,585 of them outside the per-version parse cache that the search index's span lookups use, so the search
index parsed them again. The anchors pass read and hashed each file twice and committed once per file. A full
search-index build deleted `search.db` first and wrote the new one through the ordinary WAL connection, so a
reader could meet a half-built index and a failed build left none.

### 38.2 Decisions

- detect() (vendored `project_index/detect.py`, hand-edited and marked "Verinoda patch") takes its file
  list from git when the scan root is the top of a git work tree, .gitignore rules are in play at the root
  and symlinks are not followed: `git ls-files --cached` (tracked files, which gitignore rules never drop)
  plus `--others --exclude-per-directory=.gitignore --exclude-from=<info/exclude>`, the rules the walk
  applies; the user's global excludes file is not passed because the walk never read it, and the directory
  names the walk prunes unconditionally (`_SKIP_DIRS`: node_modules, venv, build, ...) are passed as
  `--exclude=<name>/` so git does not enumerate an un-ignored one only for its files to be discarded.
  The .gitignore rules are git's own, which the Python matcher of the walk does not reproduce everywhere:
  its `*` crosses `/` (`test*.py` drops `tests/foo.py`; git keeps it), it folds case where the file
  system does while git follows `core.ignorecase`, and a UTF-16 or ANSI-code-page .gitignore the walk
  decodes is not valid UTF-8 to git. So the corpus of the git path is git's view of the tree; the walk
  runs where the difference would be large (the encoding case, see below), and the reconcile step of
  a rebuild (`ignored_predicate(gitignore=True)`) never takes a matcher-only verdict as evidence to
  evict a file git lists as untracked and not ignored: the tracked-file exemption, extended to the
  files git keeps, so a rebuild that walks after one that listed through git keeps them. Every git call
  of detect.py drops the repository-local variables a git hook exports (`GIT_DIR`, `GIT_INDEX_FILE`,
  ...), which would make `git -C <root>` describe another repository or index. The rest of what
  the walk decides is still decided the same way: noise dirs and the output dir are pruned whatever git says,
  `.graphifyignore` and `--exclude` rules (root chain and each directory's own file) are evaluated per
  directory and per file, skipped names, sensitive files, and links: an lstat per file (the realpath check
  only for a link), and a realpath once per directory, because git lists files through a Windows junction
  as through a directory while the walk's per-file realpath check kept a junction out of the root out. The
  walk still runs for a nested repository or an untracked worktree (git lists
  it as `dir/`), a `.gitmodules` file or a gitlink without one (`git add` of a nested clone records an
  embedded repository that way, and git lists none of its files), a `!` rule in .graphifyignore (it may
  re-include a file .gitignore drops, which git never lists), a .gitignore or `info/exclude` that is not
  UTF-8, a directory git cannot open (the walk names it in `walk_errors` and warns; git only warns on
  stderr), a git failure, or `enumeration="walk"`. The directory checks run in a loop, not a recursion
  (a path can be deeper than Python's recursion limit). The result says which ran
  (`"enumeration"`); `ignored` on the git path is git's report of what .gitignore dropped (a wholly ignored
  directory as one entry), which may group entries differently from the walk. A file renamed only in
  case without `git mv` on a case-folding file system keeps the index's spelling on the git path (the
  walk has the disk's); the path opens either way there, and checking the disk spelling would cost a
  directory listing per directory on every scan. Word counts stay (they feed GRAPH_REPORT.md and are
  stat-cached).
- `refresh_receiver_sidecar` parses through the per-version cache (`_PYINFO_CACHE`), so the span lookups
  of the search index that runs next in the same process reuse its parses.
- `anchors.update_facts` computes each file's facts from the bytes it already read (it read and hashed the
  file again through `facts_for`) and writes every new row in one transaction
  (`Store.put_file_facts_many`); `facts_for` is unchanged for one-off callers.
- A from-scratch search index is written to `search.db.build-<pid>` with no journal, no syncs and a 64 MB
  page cache, switched to WAL, and renamed over `search.db` after the old index's `-wal` and `-shm` files are
  removed (a WAL left next to the new file would be replayed into it). Where the old file is held open and
  cannot be replaced (Windows), the pages are copied into it with SQLite's backup API. A failed build leaves
  the old index; build files a killed build left are removed after an hour. The build file is opened for
  installing without creating it (`mode=rw`) and must hold the index's `meta` table: a build file the
  hour sweep removed while it was still being written (POSIX lets a live file be unlinked) fails the
  build and keeps the old index instead of installing an empty database.
- Not done: lexicon and anchors still parse Python separately (report item 6b), the three derived passes
  still run one after the other (6e), and definition spans are not stored in graph nodes (item 9). Every
  update still builds `ignored_predicate(gitignore=True)` in `watch._rebuild_code` for its reconcile step,
  which runs `git ls-files` and evaluates the .gitignore rules for the graph's files (and, the first time
  the rules alone drop a file, `git ls-files --others` to see whether git keeps it): part of the update
  cost the report put on detect() remains there. The parse sharing of the sidecar is bounded by
  `index._CACHE_MAX` (4,096 entries, cleared wholesale): a repository with more Python files than that
  gets part of the gain (an existing limit).

### 38.3 Measured

Machine shared with other builds (CPU load 88-100 % throughout); every wall time is an upper bound and
replicates differ by up to 2x. The django checkout was copied without its `.verinoda` folder.

- Same corpus on the trees measured (not in general: see the matcher differences in 38.2): `files`,
  `total_words` and `unclassified` (what the rebuild reads from detect()) identical, walk vs git, on seven
  trees: the django checkout, hono (TS), sidekiq (Ruby), AutoMapper (C#), jq (C; it
  has submodules, so the walk ran), the orders_app example with an ignored directory, an ignored file, an
  untracked file and a tracked noise dir added, and this repository as a linked worktree (a `.git` file).
- detect() on the django checkout, warm, under the profiler: 17.7 s (walk) -> 2.3 s (git). Unprofiled, two
  runs each: walk 16.7 / 18.7 s, git 6.1 / 2.6 s (loaded box). hono 1.6 / 1.2 -> 0.9 / 1.3 s; sidekiq
  0.8 / 0.4 -> 1.1 / 0.5 s (small trees: within noise).
- `verinoda scan` and then two one-line-edit `update`s of a fresh django copy, twice per code version, each
  from a frozen copy of the code. Wall seconds (scan / update 1 / update 2): before 281.9 / 90.0 / 110.2
  and 302.9 / 81.5 / 64.5; after 276.8 / 156.7 / 127.2 and 246.1 / 141.5 / 117.1. Phase seconds of the
  scans (index / search / lexicon / anchors): before 124.8 / 55.0 / 20.3 / 64.6 and 108.1 / 81.9 / 19.9 /
  78.6; after 154.3 / 55.0 / 27.9 / 11.7 and 146.5 / 33.5 / 31.4 / 18.6. Index phase of the updates:
  before 68.7, 61.3, 67.3, 45.8; after 102.0, 68.6, 116.1, 68.5.
- Reading: the box was at 100 % CPU with about 28 Python processes of other builds during the after runs
  (the before runs overlapped this build's own test runs instead), so the walls and the index phase are
  load, not code: the lexicon phase, which this change does not touch, is 40-55 % slower in the after runs.
  The one phase the load cannot explain is anchors: 64.6 / 78.6 s -> 11.7 / 18.6 s (one read, one commit).
  The search phase (55.0 / 81.9 -> 55.0 / 33.5 s) mixes the renamed build with the sidecar's parse reuse;
  the build file's effect alone was not isolated (an alternating build-only run was stopped because the
  box did not free up). The detect() gain shows only in the profile above: the scan JSON has no detect
  phase. An idle-box rerun of these two benches is needed before quoting end-to-end numbers. (The after
  runs used the code before the once-per-directory junction check was added: one realpath per kept
  directory, about 700 on django.)
- Tests: `tests/test_detect_git.py` (git list == walk on a repository with tracked-but-ignored, nested
  .gitignore, info/exclude, .graphifyignore dir and file, --exclude, a deleted tracked file, a noise dir,
  the output dir, symlinks where available, a junction out of the root on Windows, an un-ignored
  node_modules holding a repository, inherited `GIT_DIR`/`GIT_INDEX_FILE`; the walk for a negation, a
  nested repository, .gitmodules, an embedded repository (gitlink without .gitmodules), a UTF-16 and an
  ANSI .gitignore, an unreadable directory, no .gitignore, a git failure; a 1,100-level path; the ignore
  predicate keeps what git keeps, and a full rebuild that walks after one through git evicts nothing
  git keeps), `tests/test_search_build.py` (every table of the renamed build equals a build
  through the ordinary connection and the default location, on two examples; a held index rebuilt through
  the backup API; a failed build leaves the old index; a build file removed before it is installed fails
  the build and keeps the old index), `tests/test_line_endings.py` (no CR bytes in the package and its
  tests), `tests/test_anchors.py`
  (`update_facts` stores the rows `facts_for` stores, in one commit, over every file of `examples/`),
  `tests/test_index.py` (the sidecar's parses serve the span lookups); `tests_upstream/test_detect.py`
  and the other detect tests pass unchanged (281 passed).

## 39. Running a project's own tests safely (D63, 2026-09-28)

### 39.1 Why

A security review of the test-running paths found that process isolation was honest about its limits (the
results said the tests "can read and write files outside the copy") but that the *default* was to run any
repository's tests that way, including a freshly cloned one, and that several inputs let a repository or a
steered agent get past the argument policy:

- a repository's own `.verinoda/config.json` (force-added past `.verinoda/.gitignore`, so it arrives with a
  clone) could widen `experiments.process_isolation_allowlist` (to `python`, so `python -c ...` passed), pick
  the container image, and switch the MCP server to the full tool list;
- `python_for()` started `<repo>/.venv/Scripts/python.exe` of the original tree without any check: in a clone
  that file can be any program;
- argv[0] was checked by its basename only (`C:/anywhere/pytest.exe` passed), and a bare runner name was found
  with `shutil.which`, which on Windows looks in the current directory first (a `pytest.cmd` at the root of the
  project Verinoda runs from would start);
- pytest takes arguments the policy never saw: `@file` (pytest's parser has `fromfile_prefix_chars="@"`),
  `-o addopts=...` (nested options were not path-checked), `-p NAME` (any importable module), and the
  `addopts` and path settings of `pytest.ini` / `.pytest.ini` / `pytest.toml` / `pyproject.toml` / `tox.ini`
  / `setup.cfg` in the copy;
- the working-tree copy followed symbolic links and junctions, so a tracked link pulled an outside file's
  content into the copy and the tree hash;
- the container path ran as root, with a writable root file system and all default capabilities, gave the
  docker client a scrubbed environment (contexts, rootless sockets and `~/.docker` were lost), and had never run
  for real in the suite or CI.

For an untrusted repository no argument rule is a boundary: its tests, its `conftest.py`, `npm test`'s script,
`build.rs` are its own code and run with the user's privileges. So the decision is who trusts the code, and
the argument rules protect a trusted repository from an agent whose arguments can be steered.

### 39.2 Decisions

- **Trust is the user's decision, stored outside every repository.** `verinoda trust [path] [--subfolders] [--yes]
  [--remove] [--list]` writes `trust.json` in the per-user directory (`$VERINODA_CONFIG_DIR`, else
  `%APPDATA%\verinoda` on Windows, `$XDG_CONFIG_HOME/verinoda` or `~/.config/verinoda` elsewhere; keyed by the
  resolved path). `--subfolders` covers the folders below, never those under a `.verinoda` folder (reference
  checkouts live there), and is refused for the home folder and a drive root. No MCP tool sets trust: an agent
  steered by the repository's text cannot trust it through MCP. An agent with a shell can run the command, so
  trusting asks the user to confirm on a terminal and, without a terminal, is refused unless `--yes` is given
  (a speed bump with a clear message, not a boundary: an agent that adds `--yes` gets past it); the skill
  templates tell the agent never to run it. `--remove` works on a folder deleted since (else the stale entry
  would trust whatever is created there later) and `--list` marks such entries `missing`. A relative
  `$VERINODA_CONFIG_DIR` is ignored (it would resolve against the current directory, which can be a clone that
  ships its own `trust.json`); `~` is expanded.
- **An untrusted project's tests run only in a container.** `experiments.run` chooses process isolation only for
  an allowlisted command in a trusted project; an untrusted one goes to docker/podman when available, else it
  is refused with a `next_step` addressed to the user through the agent: "ask the user: if they trust this
  project's code, they run `verinoda trust <path>` themselves in a terminal ... an agent must never run it for
  them; or install docker/podman". Every entry point goes through `experiments.run`, so this covers `experiment run`,
  `observe`, `analyze --run-tests`, `review --run-tests`, `verify --run`, `probe`, the debug ledger and their MCP
  tools; each passes the refusal's `next_step` on (`ExperimentRefused.next_step`). Results carry `trusted`.
- **Protected settings.** `experiments.*`, `mcp.profile` and `research.network` come from the defaults, the
  user-level `config.json` in the same per-user directory, and a project's own `.verinoda/config.json` only
  when the project is trusted (`paths.PROTECTED_SETTINGS`, `load_config`, `resolve_profile`). The other settings
  still come from the project's file. What was ignored is said: in the experiment's `limits` and refusal
  (`ignored_settings_note`), and on stderr when `verinoda mcp serve` starts. Values equal to the ones in use
  (what `verinoda init` writes) are not reported. `resolve_profile` does not read an untrusted project's file at
  all, so a broken one cannot stop `mcp serve`.
- **argv[0].** A bare runner name is looked up in the absolute PATH directories only (`experiments._which`, with
  PATHEXT on Windows); when it is not there the run is an error, never a bare name handed to the OS. A path is
  accepted only for a Python interpreter this system knows (`codecheck_env.known_interpreter`: Verinoda's own,
  the registry, PATH, a Python manager's directory), a virtual environment outside the project made from one,
  or the trusted project's own `.venv` (`python_for` returns it only for a trusted project). Any other runner
  must be a bare name. The container runtime is resolved the same way.
- **pytest.** Refused under process isolation: `@file` arguments, `-p NAME` other than `-p no:NAME` and
  Verinoda's own plugins of that run (read the way pytest's `consider_preparse` reads it: `-p X` and `-pX`
  anywhere, after a `--` too; `-qpX` loads nothing and stays allowed), `-o addopts=<something>` (`-o addopts=`,
  which switches the files' addopts off, stays allowed; probe uses it). Single-dash clusters are split the way
  pytest's argparse reads them before `-o` and `-c` are looked for and before the path check (`-qoaddopts=X` is
  `-q -oaddopts=X`, `-qcFILE` is `-q -cFILE`, `-q=oX` is `-q -oX`; every letter other than pytest's value-taking
  `k m W c p o r` counts as a flag, so a plugin's flag cannot hide one). A path that names an environment
  variable (`%NAME%`, `$NAME`, `${NAME}`) is refused like an absolute one: pytest expands them in `--junitxml`,
  `--rootdir` and `cache_dir`, and the child's environment has `SYSTEMDRIVE`, `WINDIR`, `LANG`, `PATH`, `HOME`.
  The rule is lexical and applies to every runner, so any argument with two `%` (a `--grep "%s%d"`) is refused
  too (a known false positive, accepted: it fails closed; one `%`, as in a test id `[5%]`, passes).
  Path candidates are found recursively (`-o addopts=--junitxml=/x`,
  `--override-ini=addopts=--basetemp=/x`, several words in one ini value). After the copy is made, the config
  files pytest may read (the copy's root and every folder down to each path argument, and a `-c` file) are
  parsed: `addopts` gets the same check as the arguments (unless `-o addopts=`), except that `-p NAME` there is
  refused only in a file the command names with `-c` and pytest would not find itself (any file of the copy, a
  test fixture too): in the config pytest finds, it is the trusted project's own choice, like `pytest_plugins`
  in its `conftest.py` (`addopts = -p pytester` is common). `cache_dir`, `log_file`,
  `pythonpath`, `testpaths` and `pytester_example_dir` must stay inside the copy, each resolved where pytest
  resolves it (`log_file` from the working directory, the others from the file's folder). A string value is
  split like pytest splits it and, for the single-string settings (`cache_dir`, `log_file`), also checked
  whole; a TOML list item is taken as it is, as pytest does (a shell split would eat a Windows `..\`). Nothing
  is rewritten: a refusal names the file and the setting, and its `next_step` says what to change for that rule
  (a path, the `-p` of a `-c` file, the file's syntax). TOML is read with tomllib, else tomli, else a
  conservative reader (every assignment of those keys, as the strings in its value, each whole and split). The run gets
  `-p no:cacheprovider` (or, when `--lf`/`--ff`/`--sw`... need the cache, `-o cache_dir=` in the throw-away
  folder) and `--basetemp` in the throw-away folder, before a `--`, so they come after the files' addopts; the
  record keeps the caller's command in `command` and the command run in `environment.argv`.
  `analyze --run-tests` now passes `-p no:cacheprovider` like review and observe.
- **The copy follows no link.** A working-tree file that is, or lies under, a symbolic link or a junction is not
  copied and is listed in `source.skipped` (with a limit line), as the commit copy already did. Read from one
  directory listing per folder; on Windows only symlink and mount-point reparse tags count (cloud placeholder
  files are ordinary files). Best effort: a folder whose listing cannot be read is not checked.
- **`verify --run`** in a project whose re-run is refused verifies as without `--run` and says so (`run.refused`,
  `run.next_step`); a run that did not happen neither confirms nor refutes the claim.
- **Container hardening.** `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges`, the host
  user (`--user uid:gid`; rootless podman `--userns=keep-id`, because `--user` there is a sub-uid the user
  cannot delete; Windows `--user 1000:1000`), `HOME=/tmp`, `PYTHONDONTWRITEBYTECODE=1`. The docker/podman client
  gets the real environment's daemon settings (`DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CONFIG`,
  `CONTAINER_HOST`, `XDG_RUNTIME_DIR`, the real HOME, ...; `CLIENT_ENV_ALLOW`), never the secrets the scrubbed
  environment drops; the container gets only the `-e` values. The image is `experiments.container_image`
  (user config or a trusted project), default `python:3.12-slim`, which has no pytest: such a run is
  `inconclusive` with a `next_step` naming the setting and the user config file.
- **Tests and CI.** The test suite points `VERINODA_CONFIG_DIR` at a temporary folder and trusts its temporary
  folder with subfolders (tests/conftest.py), the way a user would; tests of the untrusted path use a fresh
  per-user folder. `tests/test_experiments_container.py` runs the container path for real and skips unless
  `VERINODA_CONTAINER_TESTS=1`; the CI job `container` (ubuntu-latest, real docker, an image with pytest built
  in the job) sets it, and there a missing runtime fails instead of skipping.

What remains (not done here, R8/R9 of the review): **OS-level confinement without Docker.** An untrusted project
on a machine without docker/podman is refused, not confined. Candidates, each to report what it covered in
`guarantees` and to fall back to "refuse", never to unconfined: Linux bubblewrap (`bwrap --unshare-all`, binds
of the interpreter and the copy) or Landlock; macOS `sandbox-exec` with a deny-default profile; Windows a
low-integrity token with a low-integrity copy folder (writes), AppContainer (network and reads). Also open:
Windows job-object memory and process-count limits (`resource_limits` is still false on Windows under process
isolation), and trust keyed by the remote URL as well as the path.

### 39.3 Measured

Decision functions called directly, before and after (Windows, Python 3.12, pytest 9.1.1; no child process):

| Input | Before | After |
|---|---|---|
| `python -m pytest @args.txt` | allowlisted | risky (@file) |
| `python -m pytest -p someplugin` | allowlisted | risky (-p) |
| `C:/anywhere/pytest.exe -q`, `C:/anywhere/npm.exe test` | allowlisted | risky (argv[0] by path) |
| `python -m pytest -o addopts=--junitxml=C:/x.xml` (and `-oaddopts=`, `--override-ini=addopts=--basetemp=C:/x`) | allowlisted | risky (absolute path) |
| `python -m pytest --junitxml=C:/x.xml` | risky | risky |
| `npm test` | allowlisted | allowlisted (runs only in a trusted project or a container) |
| repo config `{"experiments": {"process_isolation_allowlist": ["python"]}, "mcp": {"profile": "full"}, "research": {"network": "on"}}`, project not trusted | allowlist `['python']`, profile full, network on | defaults, profile core, network cache (reported as ignored) |
| `python_for` with `<repo>/.venv/Scripts/python.exe` present, project not trusted | the repository's file | Verinoda's own interpreter |
| `zzvnprobe.cmd` in the current directory, not on PATH | `shutil.which`: `.\zzvnprobe.CMD` | `_which`: not found |
| `python -m pytest --junitxml=%SYSTEMDRIVE%/Users/Public/x.xml` (also `--rootdir=%SYSTEMDRIVE%/`) | allowlisted (the review wrote a file outside the copy through `%LANG%`) | risky (environment variable) |
| `python -m pytest tests -- -pX`, `-qoaddopts=-pX`, `-qo addopts=-pX` | allowlisted (the review's runs imported X) | risky (-p / addopts override) |
| `python -m pytest -qcsub/evil.ini` with `addopts = -pX` in that file | ran (X imported) | refused (the `-c` file's addopts) |
| trusted project, `pytest.ini` `addopts = -p pytester` | refused without a container (a regression of this branch) | runs with process isolation |
| `pyproject.toml` `addopts = ["--junitxml=..\\..\\out\\x.xml"]` | allowed (shell split ate the backslashes; the review wrote the file outside) | refused ('..') |

- Link check of the copy: 121-170 ms for 2,561 files in 335 folders (this repository, Windows, one directory
  listing per folder, five runs); the copy itself took 7-43 s on the same (shared, loaded) machine, so the check
  is a few percent at most.
- Tests: tests/test_experiments_trust.py (84 tests: protected settings, trust store and command (confirmation,
  `--yes`, a deleted folder), refusal and next_step per entry point (verify --run included), container command
  and client environment through a fake runtime, argv[0], `_which` without the current directory, the pytest
  argument and config-file rules (environment variables, `-p` after `--`, clusters, `-p` in a `-c` file only, TOML
  list items), a real pytest run showing it reads those spellings, the TOML fallback, `--basetemp` in the
  throw-away folder, links not followed, a relative `VERINODA_CONFIG_DIR`, an untrusted project's broken
  config and `mcp serve`); tests/test_experiments.py updated for the argv[0] rule; tests/test_agents.py checks
  the templates' trust and profile lines.
- Not measured here: the container path itself (no docker/podman on this machine). The CI job `container` is its
  first real run; the flags follow the docker and podman documentation.

## 40. Issue-shaped questions: never refused for the drafted plan, answered in the question's language, restated briefly (D67, 2026-09-28)

### 40.1 Why

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

### 40.2 Decisions

- **The draft carries every version, whatever the limit** (`question_plan._draft_references`,
  `_fold_overflow`). A version named again (the same SHA or pull-request number twice, a short SHA after its
  full SHA) is one reference. Only the same version is merged: `2.0.1` is not `2.0.10`, `#12` is not `#123`
  nor the `12` of `10.11.12`, and `scipy 1.23.5` is not `numpy 1.23.5` (`_same_version`, equality per kind).
  When more references remain than a plan may hold, the versions of the user's own sentences stay references
  of their own first, then those of pasted blocks (fenced code, lines that only pair a name with a version);
  one last reference (`derived_by: ...:version_overflow`) keeps the user's own words as its `text` and lists
  every remaining version in `version.evidence`, where the version check finds them. The draft lists it under
  every sub-question that names one of its versions, and the "the question names ..." note counts each of
  them. None is dropped and the plan stays within its limits. A host's plan that drops a version is still an
  error, and the check now needs the version as a whole token (`_carried`): a plan carrying `2.0.10` or `#123`
  no longer passes for `2.0.1` or `#12`. Relative versions ("the previous release") always stay references
  of their own, because a version clarification is about them.
- **A typed question is never refused for its drafted plan** (`analysis.analyze`, `question_plan.fallback`).
  When the drafted plan still fails its checks, it is stored as it was (status `invalid`) and the question is
  answered with a fallback plan: the whole message as one sub-question (intent from the rule cues) about the
  drafted plan's mentions, without references, stored with the drafted plan as its `parent_id`. Without the
  mentions every word of the question would look absent from the repository (the "words occur nowhere" check
  reads the plan's links); when the mentions are what failed, the sub-question has none. A plan without
  references cannot carry the message's versions, so for this plan only (`check(..., refuse_versions=False)`)
  a dropped version is a warning: there is no plan the user could fix. A blank question is still refused
  (`invalid_plan`): the fallback's own plan must pass every other check. The answer says so: the result has
  `plan_fallback` (the drafted plan's id, why, its first errors), the text view prints a `note:` line after
  "understood as", and the MCP view carries `plan_fallback`. A plan the host passed (`--plan`, MCP
  `plan_json`) that fails its checks is still refused with `invalid_plan` and exit 2, unchanged.
- **The language is the prose's** (`textnorm.detect_language`). Only prose counts: fenced and inline code and
  URLs are left out (all of the text counts when nothing else is left), and the tails of English contractions
  (`'s 't 'd 'm 've 're 'll`) are dropped before the text is split into words; a Turkish suffix after an
  apostrophe (`API'de`, `Order'ı`) is not in that list and still counts. When both languages show, a message
  whose English function words number at least three and at least three times its Turkish signals (Turkish
  function words plus words with Turkish letters) is English, unless the user's own question, the first or
  the last sentence of the prose (`>` quotes left out), is asked in Turkish (a Turkish question word or two
  Turkish function words, and more Turkish signals than English function words in that sentence). A Turkish
  question about a pasted English issue, log or error ("Bu hata neden oluyor?" over an English log), or with
  English identifiers or an English phrase in it, stays "mixed" and is answered in Turkish as before. A lone
  triple backtick in the middle of a sentence, not closed on its line, opens no code block. The intent cue
  tables still use their own test (`has_turkish`, unchanged: it feeds the lexicon at scan time, and changing
  it would change the index).
- **The question is restated briefly.**
  - An ordinary question is restated whole, as before, however many clauses it has ("Understood (rules): q1
    [locate] Where is compute_total defined?"). Only an issue-sized message (its sub-questions' text over 600
    characters, `ISSUE_CHARS`, or pasting lines or a fence) gets a drafted `restated_goal` /
    `restated_goal_user_lang` of at most 300 characters (`GOAL_CHARS`) on one line: every `qN [intent]` head
    is kept, a text that fits its share is whole and leaves the rest to the longer ones, a clause is cut in the
    middle (`Which functions … apply_discount?`: its subject, and a Turkish predicate, are at its end), a text
    pasting lines is shown by its first line, and the glosses get what the texts leave. A host's goal is shown
    as the host wrote it. The "Understood as" contract of the answer is unchanged.
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

### 40.3 Measured

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

## 41. Verinoda's own files are not the project's: no index of them, no rebuild for them (D68, 2026-09-28)

### 41.1 Why

`verinoda setup` ran `scan`/`update` first and wrote the agent files after it: the skill
(`.claude/skills/verinoda/SKILL.md`, for Codex `.agents/skills/verinoda/SKILL.md`) and the MCP server entry
(`mcpServers.verinoda` in `.mcp.json`, `[mcp_servers.verinoda]` in `.codex/config.toml`). Those files were
part of the project's corpus like any other: the snapshot listed and hashed them, the graph had 16 heading
nodes for the skill and, for `.mcp.json`, a `verinoda` server node and an `mcp_command` node whose label was
the absolute path of the interpreter that ran setup (a user path, in the graph and the search index).

So the next setup or update found them changed whenever their text differed, and a changed file the graph
has nodes from rebuilds the whole graph (`workflow._graph_affected` -> `index.build`). The rendered skill and
the MCP entry name the interpreter that ran setup, so two installs of Verinoda running setup in turn (a
global tool and a development checkout, the MCP server's and the terminal's) always differed. On a
2,623-file project a setup that changed nothing of the project took 22-40 s instead of about 1.5 s whenever
another install had run it last (measured below). The run after a first setup paid it too: setup's own
writes were "new" to it. The
files also put Verinoda's instructions into the project's search index, lexicon and graph, where they
answered questions about the project.

### 41.2 Decisions

- **One rule for "Verinoda's own file"** (`verinoda/selffiles.py`, new): a file that carries the ownership
  marker (`<!-- verinoda-managed`, the installer's own test of "managed by Verinoda") and is
  - under a folder of the shape the installer writes its skills to, `.claude/skills/verinoda/` or
    `.agents/skills/verinoda/` (`SKILL_DIRS`, which the installer now takes its skill paths from, so the two
    cannot drift), at any depth: a package of a monorepo set up on its own has its skill at
    `pkg/.claude/skills/verinoda/`, and the monorepo indexed at its root must leave it out too. The folder
    names are compared in any letter case: on a case-insensitive file system the installer's `.claude`
    lands in an existing `.Claude`;
  - or listed by the project's install manifest (`.verinoda/install-manifest.json`) as written whole by
    Verinoda (`kind: "file"`). Today those are the skills; the rule covers any later installer target
    outside the skill folders.

  The marker decides, not the folder and not the manifest. A file without it is the user's, as the
  installer treats it: a `SKILL.md` it refuses to overwrite ("not managed by Verinoda"), one the user took
  over by removing the marker, a `reference.md` or script the user keeps next to the managed skill. They
  stay indexed. No manifest entry is required, so a teammate's fresh clone of a project with a committed
  skill (no local manifest yet) leaves it out as well. The manifest is read with every error caught (a
  malformed manifest lists nothing) and remembered by its size and time, as is each file's marker check,
  because the file list is taken many times in one analysis; a path is split and stat'ed only when it
  contains `skills/verinoda/` or is listed.
- **Applied where the corpus is listed, not per consumer:**
  - `snapshot.list_files`, tracked or not (a team may commit its skill). Everything that reads the file
    list inherits it: the snapshot and its stale-claim diff, the search index's data files, the lexicon's
    candidates, the syntax facts, experiment copies, the UI and `treestate.current`.
  - The commit side of `treestate` applies the same rule to the same paths (the working tree's verdict):
    `commit_entries`/`commit_files` (commit copies, the ids of a commit-sourced attempt), `base_ids` (kept
    whole on disk, filtered when read, because which files are Verinoda's depends on the working tree
    now), `changes_from_ids`, `changes_vs_base` and `changes_between_commits`. Without it a committed skill
    and a clean tree read as a deleted file in every debug-ledger attempt: `tree.changed_during_run`, a
    deletion in `vs_base` and `change.patch`, and a `code_tree_id` that differed from a commit-sourced
    attempt of the same code. A skill the user deleted or took over is compared like any other file.
  - The graph build (`project_index/watch._rebuild_code`, Verinoda patch): one literal `--exclude` pattern
    per own file present (`selffiles.ignore_patterns`, anchored at the project root; no folder pattern can
    say "carries the marker"). The files are found with one `git ls-files` limited to the skill folders at
    any depth and in any case (`:(glob,icase)**/.claude/skills/verinoda/**`), outside git with the walk
    `list_files` uses, plus the root's own skill folders read from disk whatever git ignores. detect() then
    never reads them, on the walk and on the git-listing path alike, and explicit patterns win over
    git-tracked status. The same patterns feed the reconcile's "a live ignore rule matches" test, so the
    nodes an older graph has from those files are evicted on the next build instead of being kept by its
    fail-closed rule.
  - `freshness.check`: it walks known folders itself and asks git about new files; the root is always a
    known folder, so without the rule a skill would be reported as "changed since the index" on every read,
    and no update could clear it.
  - As the last line of defence, `index._post_process` (which already drops the nodes of missing files)
    drops the nodes of Verinoda's own files from graph.json after every scan/update build and reports them
    as `own_files_dropped`. A path is resolved only when it could be one (it names a skill folder or a
    listed file): resolving every file took 0.44-0.50 s per build on Verinoda's own graph (29,882 nodes,
    1,236 files), the check now 0.002-0.003 s. Needed in practice: an index copied from another folder
    records that folder as the graph's root (`.graphify_root`), and the reconcile then keeps every stored
    node whose file looks outside the scanned folder; on the copy of the 2,623-file project the skill's 16
    nodes survived the exclude rules until this step was added.
- **Shared agent configs stay in the corpus.** `.mcp.json` may hold the user's other servers; one key that
  Verinoda wrote does not make the file Verinoda's, and leaving it out would drop the user's servers from the
  graph and the search index the moment setup ran. What changes:
  - Verinoda's own entry never shapes the graph: the MCP-config extractor (`mcp_ingest`, Verinoda patch)
    skips a `verinoda` server started as `... mcp serve` (`selffiles.is_own_mcp_entry`), so the interpreter
    path is no longer a graph node. `.mcp.json` keeps its file node and the user's servers.
  - A change confined to that entry does not rebuild the graph. The graph build records, per MCP config in
    the listing, a digest of the file as the extractor reads it with Verinoda's entry left out: its JSON with
    keys sorted, whitespace and key order not counting (`selffiles.config_digest`; a file the extractor cannot
    parse is digested byte for byte). It is taken before the build reads the files, so a config edited during
    a build differs next time instead of passing for what the graph read, and stored in `build_stats.json`
    (`configs`) by `buildlock.record_build`. `_graph_affected` skips a modified MCP config whose digest equals
    the recorded one; the snapshot records its new bytes and the search index re-indexes it (one file).
  - Not chosen: a snapshot hash with Verinoda's entry masked. `critique`, `runtime.trace` and `lexicon`
    compare `sha256_file` against snapshot hashes, so a masked hash would read as a change there forever.
  - `.codex/config.toml` has no graph extractor (`classify_file` gives it no type), so a change there never
    rebuilt the graph; it is still re-indexed for search on change, and its Verinoda block stays searchable
    text. Left as it is.
- **setup writes the agent files first, then indexes** (`setup.setup_project`): init, `--reference`, the
  installer for each agent, then scan/update. The snapshot that setup records already has whatever setup
  wrote, so the next setup or update never meets setup's own writes as a change even for a file the rule
  does not cover (the rest of `.mcp.json`). The report keeps its shape and order; `index.graph` is new:
  `"full"` when the graph was built, `"none"` when it was left as it was. An installer that raises an
  `OSError` (a folder named `.mcp.json`, a config this user cannot read) is that agent's error in the report
  (`ok: false`, `error`), and the project is indexed all the same, as it was when setup indexed first. The
  agent files stay installed when the scan fails or is refused; the MCP server answers `no_index` until a
  scan succeeds.
- **The AST cache schema is 8** (`project_index/cache._AST_CACHE_SCHEMA`): an unchanged `.mcp.json` would
  otherwise come from the AST cache (keyed by content and schema) with the Verinoda node the old extractor
  made. Adding `mcp_ingest.py` to the extraction stamp would not do: it forces a rebuild, but the rebuild
  would take the old extraction from the cache. The schema is also part of the extraction stamp, so the
  first update after the upgrade rebuilds the graph once, from a cold AST cache.
- Existing projects need nothing: on the next update the own files leave the file list, the snapshot diff
  lists them as removed, the graph is rebuilt (removed files the graph has nodes from), their nodes are
  evicted, and the search index drops the files that left the graph and the list. A later update is a no-op.
- Not changed: `verinoda install` on an already indexed project that creates `.mcp.json` adds a JSON file,
  which counts as code for the graph: one rebuild, once (so does the first `setup` on a project indexed with
  `scan` before). User-scope installs write under the home folder and are not in any project's corpus.

### 41.3 Measured

Before / after on a copy of a 2,623-file project (a Java project with Markdown and JSON data; a git work
tree), `python -m verinoda setup . --agents claude --json` from this branch's code, run in this order. A is
one virtual environment, B a second one with the same code installed editable (another interpreter path, as
a global tool and a development checkout differ). Each series starts from a fresh copy of the same project
and its existing index; "settle" is the first run on it (that index was built by an older build, so both
first runs rebuild the graph). Wall-clock seconds on a Windows 11 machine, one run each:

| run | before | after |
|---|---|---|
| settle (A) | 31.5 (graph rebuilt) | 32.4 (graph rebuilt, cold AST cache) |
| A again | 40.2 (graph rebuilt: setup's own writes of the settle run) | 1.30 (no-op) |
| A again | 1.49 (no-op) | 1.44 (no-op) |
| A again | 1.35 (no-op) | 1.85 (no-op) |
| B | 2.25 (no-op: B's writes came after its update) | 5.47 (graph not rebuilt) |
| A | 23.2 (graph rebuilt) | 5.35 (graph not rebuilt) |
| B | 22.6 (graph rebuilt) | 5.09 (graph not rebuilt) |
| A | 22.4 (graph rebuilt) | 5.13 (graph not rebuilt) |
| `update` | 21.7 (graph rebuilt for the skill and `.mcp.json` of the last setup) | 0.82 (no-op) |
| `update` | 0.74 (no-op) | 0.76 (no-op) |
| one-line `README.md` edit, `update` | 22.5 (graph rebuilt) | 26.6 (graph rebuilt) |
| `update` | 0.68 (no-op) | 0.70 (no-op) |

The graph had 9,477 nodes before any setup; with the old code setup's files added 19 (9,496: 16 skill
headings, the Verinoda server and its interpreter path); after, 9,478 (the `.mcp.json` file node). Two more
setups on the "after" copy, from A and from B, reported `index.graph: "none"` with the agent result
`updated`.

What the 5 s of an alternating setup are (profiled, one setup from B: 7.9 s under cProfile): the update's
derived-data refresh for the one changed file, `.mcp.json`: `lexicon.build` 3.6 s, of which `_associate`
2.8 s re-associating units none of which changed (`.mcp.json` is not a lexicon input), and
`search_index.update` 1.4 s (a stat of every indexed file, the file list). Not changed here (see Not done).

Existing project, in place (the "before" copy after its runs above: skill and Verinoda's `.mcp.json` entry in
the graph, 9,496 nodes): the first `update` with the new code took 28.0 s (`index_mode: full`: the skill
listed as removed, plus the new extraction stamp), leaving 9,478 nodes (the 16 skill nodes and the Verinoda
server and interpreter-path nodes gone; the `.mcp.json` file node kept); `freshness.check` 0; the next
update a no-op in 0.72 s. The same state copied to another folder (graph root recorded for the first
folder): 34.4 s, the skill's nodes dropped by the post-processing step, freshness 0, then a no-op in 0.73 s.
There the Verinoda server node from before D68 stayed (the reconcile keeps nodes it judges outside the
scanned folder, a property of copied indexes that predates D68; deleting `.verinoda/index` and scanning again
clears it).

Side question, not changed: a one-line edit of `README.md` rebuilds the whole graph today: `update` took
22.5 s (`index_mode: full`, 1 file changed) on the same project, against 0.7 s for a no-op update. Markdown
files are graph input (their headings are nodes), so `_graph_affected` counts them as graph files; its
docstring said an edited README is only re-indexed for search, which is now corrected. A document is
extracted on its own (no cross-file pass reads its headings), so re-extracting just that file, as the
MCP-config digest does for Verinoda's entry, could avoid the rebuild; that is a separate change.

Tests: `tests/test_selffiles.py` (new): the installer's skill paths are the rule's folders and the manifest's
place is the rule's; own files are never listed, tracked or not, while another skill, a manifest-listed file
without the marker and `.mcp.json` stay; a malformed manifest does not break the listing; the own-entry test
and the digest (Verinoda's entry, whitespace and key order do not count, another server does, an unparsable
file is digested by bytes); the extractor makes no node from Verinoda's entry or its interpreter path; setup
twice with one interpreter is a no-op, then three setups alternating two interpreters leave the graph build
record untouched (`index.graph == "none"`), the skill in neither graph nor search index, freshness 0; a user's
edit of another server in `.mcp.json` still rebuilds the graph, which then has that server and not
Verinoda's; an index built with the old rule drops the skill from snapshot, graph and search index on the next
update and the update after is a no-op (checked to pass through the exclude rules alone); an index whose
graph root names another folder holding the same files drops them too (fails without the post-processing
step); the real CLI run twice is a no-op. From the review: a committed skill with a clean tree gives no
change, no drift and no deletion in `changes_from_ids`/`changes_vs_base`/`changes_between_commits`, and a
commit-sourced tree equals the working tree's (a skill taken over is compared like any other file); a
package's skill in a monorepo indexed at its root is not listed, has one exclude rule, never reaches the
graph or search, and another interpreter's setup of the package rebuilds nothing at the root; a marker-less
`SKILL.md` the installer refuses and a `runbook.md` in the skill folder stay listed, in the graph and in
search, and once the user hands the folder to Verinoda only the managed `SKILL.md` leaves; a `.Claude` case
variant is left out while `.Claude/settings.json` stays; a folder named `.mcp.json` fails the agent and the
project is still indexed. Each fails on the code before the review. Existing setup, snapshot, agents,
workflow, detect, freshness, portable-id, docs and packaging tests and the vendored MCP-config tests pass
unchanged.

### 41.4 Not done

- The derived-data refresh of an update whose only change is a file no derived index reads much of (here
  `.mcp.json`) still costs about 3.5 s on this project: `lexicon.build` re-associates all units whenever any
  file changed. Skipping that when no lexicon input changed needs a restamp of the lexicon's `tree_hash`
  (analyze rebuilds a lexicon whose tree hash is not the snapshot's) and checks for locale files, the seed
  dictionary and the parameters; it is a change of its own.
- An edited Markdown file rebuilds the whole graph (the side question above).
- In an index copied from another folder, a node the old extractor made from Verinoda's `.mcp.json` entry
  can stay (see Measured); a fresh scan clears it.
- The first `setup` on a project indexed with `scan` before creates `.mcp.json`, a new JSON file, and so
  rebuilds the graph once. Skipping it would need the extractor to make no node for a config that holds no
  server of the user's, and `_graph_affected` to treat any MCP config whose digest differs from the
  recorded one as affecting, in the graph or not.
- The search index reads `.mcp.json` as it is, so the interpreter path in Verinoda's entry (a user name)
  is still searchable text there; only the graph leaves it out.
- Experiment and commit copies leave Verinoda's own files out, committed or not (the same tree on both
  sides of a diff). A project whose own tests read its committed, managed skill would fail in a copy.

## 42. Datapack functions called from Java (D69, 2026-09-28)

### 42.1 Why

`verinoda datapack function ns:path` listed only the callers in `.mcfunction` files, although the command's help
said "one tag, score or function across mcfunction and Java". In a mod the question "who calls this function?" is
usually about Java: where the game actually triggers it. The bug that raised the issue was of that kind: a
projectile weapon ran a function that acts on the one entity the datapack keeps track of instead of the body it
hit, and the answer to "who calls it?" named one mcfunction caller and neither of the two Java callers that
mattered. A
function run only by Java command strings was reported as "no call in or out found". A Java call to a function
that does not exist is a silent bug (`getFunctions().get` returns nothing and nothing happens), and the summary's
"calls to functions that do not exist" did not look at Java. D52 had listed "Java running a function by a built
string" as not done.

Java runs a datapack function in three ways, and all three occur in the mod that raised the issue:

1. a **command string**: `performPrefixedCommand(src, "function ns:x")`, `runCommand("execute ... run function
   ns:x")`, Bukkit's `dispatchCommand(console, "function ns:x")`;
2. an **identifier** handed to the function manager: `server.getFunctions().get(Identifier.fromNamespaceAndPath(
   "ns", "x"))`. The same `fromNamespaceAndPath` builds item models, textures, dimensions and registry keys, so the
   identifier alone proves nothing;
3. a **helper** that adds the namespace: `static void runFunction(ServerPlayer p, String name)` whose body is form
   2 with `name` as the path; the code then only says `runFunction(p, "x")`. The mod has three same-named helpers
   (a static one with two parameters, a private one with three, a command-context one taking `ns:x` or `x`), so a
   helper cannot be recognised by its name.

### 42.2 Decisions

- **`verinoda/datapack_java.py`** reads the Java files with tree-sitter (the Java grammar is already a dependency).
  A file is parsed only when its text names the function manager (`getFunctions`, `getCommandFunctionManager`)
  or holds a string with `function ` in it, or calls a helper found so far; the nodes are found from the text
  (the regex's byte offset, then the node there), not by walking every node. `verinoda datapack` and `datapack
  function` run it; `datapack tag` / `score` do not.
- **A command string must read as a command.** A `+` concatenation is joined into one text, a part known only at
  run time (`p.getName()`) kept as a placeholder, a `static final String` constant filled in, a text block read
  line by line. The text must start as a command (`function`, `execute`, `schedule`, `return`, a leading `/`
  allowed) or with a run-time part followed by an `execute` step (`... + " run function ns:x"`); after the id only
  the end, macro arguments (`{...}`, `with ...`) or, for `schedule`, a time may follow. So a log format
  (`"function ns:{} does not exist"`), a chat hint (`"Cleanup: /function ns:x"`), a sentence that mentions a
  command (`"the /execute ... run function ns:x retry"`) and a comment are not calls. The record says how:
  `function`, `execute run` or `schedule`.
- **An identifier counts only in the function manager's lookup.** A `get(...)` / `getFunction(...)` whose receiver
  is `getFunctions()` / `getCommandFunctionManager()` or a local holding it (`ServerFunctionManager functions =
  server.getFunctions()`, by its initialiser or its declared type); its argument is the identifier, inline or
  through a local assigned once (`Identifier id = ...; get(id)`). Identifiers: `Identifier` / `ResourceLocation`
  `fromNamespaceAndPath`, `of`, `tryBuild`, `parse`, `tryParse`, `withDefaultNamespace` and `new ...(ns, path)`,
  fully qualified or not, over several lines. The same identifier as an item model or a registry key is not a
  call.
- **A helper is recognised by its body**: a method whose String parameter reaches that lookup with a constant
  namespace (`fromNamespaceAndPath(NS, name)`, `"ns:" + name`, `"ns:prefix_" + name`, the whole-id-or-path form
  `name.contains(":") ? name : "ns:" + name`), or completes the name in a command string (`"function ns:" +
  name`, `String.format("function ns:%s", name)`). A helper is its own declaration (file and position), so two
  same-named helpers, or two same-named classes in two packages or two trees, stay apart. A call binds to a
  helper only when Java would resolve it there: a qualified call (`Mod.runFunction(...)`, `pkg.Mod.runFunction`,
  the declared type of an instance receiver) when the caller's file can see that class (the same file or
  package, `import pkg.Mod;`, `import pkg.*;`, a fully qualified name, `Outer.Inner` for a nested class); an
  unqualified call when the innermost enclosing class that declares a method of that name declares the helper
  with that argument count (a same-named method of the caller's own class is not the helper); or a static
  import of it. A helper found by its body binds only calls in its own tree: the reference tree may repeat the
  product's class and package names. A method that passes its own parameter to a helper is a helper too (a few
  rounds), an overload that delegates to the helper (`runFunction(p, name, delay)` calling `runFunction(p,
  name)`) included; only a call in the helper's own body is not. Every call of a helper with a constant (a
  literal, a constant, a local assigned once from one) is a call of the function the helper builds from it; so
  is a call inside a loop over a constant table (`for (String[] e : TABLE) ... e[1]`, `final String fn = e[1]`,
  `for (String n : List.of(...))`, the table in a local or a final field of the file): one record per distinct
  function, at the line of the call. `datapack.function_helpers` (or `function-helpers`) in
  `.verinoda/config.json`, `["Class.method:argIndex:namespace"]`, names a helper whose body is beyond this
  reading.
- **A name built at run time is reported, never dropped** (`"give_" + kind`, a loop over names from elsewhere, a
  command argument, `String.format("function ns:%s", kind)` / `.formatted`): the summary lists each site once as
  `dynamic: File.java:N builds ns:give_*` (or "builds the whole name at run time"), and `datapack function
  ns:give_sword` lists the dynamic sites whose known part fits it as "may be this one" (also when the datapacks
  do not have the function). A bare `ns:*` (or a whole id built at run time) fits every function: the function
  view gives one line naming those sites ("N Java site(s) build the name at run time and may run this one too")
  instead of saying "no call in or out found".
- **Lines.** A helper call is reported at the line of its name (a chained call split after `Mod` at the line
  of `.runFunction(`); each command of a text block at its own line. `execute if|unless function ns:x run ...`
  runs `ns:x` as the condition and counts, as it does in an mcfunction file. A `static final Identifier` field
  handed to the lookup is read like a local.
- **Cost.** A file is read for helper calls only when its text calls one of the helpers' names (not the name
  inside another word) and names that helper's class, so a helper called `get` or `run` does not make every
  file with `.get(` a candidate.
- **Output.** `datapack function` lists each Java call as its own record after the mcfunction callers:
  `called by Java Class.method at path/File.java:N (helper Mod.runFunction)` / `(command string, execute run)` /
  `(identifier)`, with `[test]` for a test file and `[reference tree NAME]` for a file under a configured reference
  tree (`index.reference`, `setup --reference`: the same rules, labelled with the tree's path); product code first,
  then tests, then reference trees. JSON: `called_by` rows carry `kind` (`mcfunction` or `java`); a Java row has
  `via` (`command-string`, `identifier`, `helper`), `helper`, `caller`, `at`, `how`, `target`, `text`, and `tree`
  / `test` when they apply; `dynamic` lists the run-time names that may be this function and `dynamic_any` the
  sites that build all of the name past the namespace. "no call in or out found" is said only when there is no
  mcfunction caller, no Java caller, no event and no dynamic site that fits. A function the datapacks do not have
  but Java calls prints its Java callers (and the fitting dynamic sites) under "not found" (exit 2).
- **Summary.** The head counts the Java calls and, when some are in tests or reference trees, how many are in
  product code; "calls to functions that do not exist" includes a Java call whose
  target is missing, in a namespace the datapacks define (another mod's functions and `minecraft:` ids are not
  flagged); a line gives the Java calls by kind (and how many are in tests and reference trees) and the helpers
  recognised; the names built at run time follow. JSON: `java` (`calls`, `by_via`, `in_tests`,
  `in_reference_trees`, `helpers` with where each was found and why, `dynamic`, `unresolved_lookups`).
- **Not in the graph.** The Java calls are read at query time and are not graph edges: `when`, `trace` and impact
  still follow only the mcfunction calls.

### 42.3 Measured

On a copy of the private mod that raised the issue (one datapack namespace, 314 functions; 425 Java files of which
41 in a configured reference tree, the Paper plugin it was ported from), the real CLI:

- **Before** (849ca63): no `datapack function` answer listed a Java caller. For the four functions the issue
  names, 0 of their 7 Java callers were listed, and one of the four was "no call in or out found".
- **After**: 68 Java calls bound to a function: 23 in product code (helper 18, command string 3, identifier 2), 34
  in tests (helper 31, command string 2, identifier 1), 11 in the reference tree (command strings). 8 names built
  at run time are listed as dynamic (5 in product code, 1 in tests, 2 in the reference tree); 0 lookups of an id the
  text does not spell. 10 helpers recognised by their body, none configured: in product code the static
  two-parameter helper, the private three-parameter helper of the same name, the command-context helper and one
  method that hands its parameter on; 6 in tests (one takes the function as its fourth of six parameters).
  9 of the 68 come from constant tables: the stage command's "spawn" verbs (a local `String[][]` of 7 rows naming
  4 functions, run through the command-context helper) and a test's `List.of` of 5 function names; before the
  review fixes they were 2 bare `ns:*` dynamic sites. The other 59 calls and their lines did not change.
- **The four functions**: 7 of 7 Java callers listed with file:line and the calling method (the two callers
  through the static helper, the three command strings, the one identifier lookup, the one call through the
  private three-parameter helper), plus 1 test caller and 5 reference-tree callers of the same functions.
- **False positives**: every one of the first 59 bound calls read by hand, and the 9 table rows checked against
  the tables: 0 wrong, and all 68 targets exist (0 missing functions from Java). The lines that must not count do
  not: identifiers built for item models and a registry lookup of an identifier, a sentence that mentions `run
  function`, a log format string, a chat hint with `/function`, and comments that quote a command. The issue
  counted "~40" by text search (helper calls, every `fromNamespaceAndPath` of the namespace, every `function ns:`
  string); in product code the syntax gives 20 helper call sites (14 with a constant, 1 over a constant table, 4
  built at run time, 1 that forwards a parameter), 3 identifier lookups (the
  other matches build item models, textures or dimensions, or are the helpers' own bodies) and 3 command strings
  (the other matches were a comment, a doc comment and a chat hint).
- **Time**: `verinoda datapack` 2.28 s -> 2.60 s, `datapack function` 2.13 s -> 2.49 s (median of three, Windows);
  31 of the 425 Java files are parsed, 0.27 s. The first version walked every node of 46 files (0.9 s); finding
  the nodes from the text instead gave the same 59 calls and 10 dynamic sites. The review fixes keep the 31 files
  (the scan 0.26 s -> 0.29 s in process; the CLI's change is inside its run-to-run noise). With one extra helper
  given a common name, the files read went from 142 / 229 / 273 / 371 (`fire` / `run` / `add` / `get`) to 32 /
  32 / 45 / 45.

### 42.4 Not done

Java calls as graph edges (`when` / `trace` from a Java method to a function; the MCP `analyze` / `node_inspect`
still see the mcfunction callers only), Kotlin, advancement rewards and predicates. A table built by code rather
than written out (`map.put(...)` in a loop, a table in another file) is still dynamic; a helper inherited from a
superclass is not bound on an unqualified call (the subclass has no declaration of it). A command string is reported where its text is
(a `static final String` command at its declaration, not at each dispatch), and a string that holds a command is a
call whether or not it reaches the dispatcher (`assertEquals("function ns:x", s)`); an existence check
(`getFunctions().get(id).isPresent()`) counts as a lookup.

## 43. Entity tags Java adds through a constant, a conditional, the live set or a built name (D70, 2026-09-29)

### 43.1 Why

GitHub issue #2: in the mod that raised it, 4 of the 5 rows of "tags checked but never added" that were checked by
hand were false alarms, and the fifth (a tag set by hand with `/tag`) was right. The list is there to find one kind
of bug (a tag something checks and nothing adds); when most rows are wrong, every row has to be checked by hand and
the list stops being used. The four adds it missed:

1. **A class's own constant.** `sakin.addTag(ETIKET)` with `public static final String ETIKET = "croat"` in the same
   class. The constants were one table by simple name, and a name two classes give different values was dropped;
   `ETIKET` ("tag") is the name every class of that mod gives its own tag, so the constant was never read.
2. **A conditional.** `vucut.addTag(koyluydu ? ESKI_KOYLU : ESKI_SAKIN)` adds one of two tags.
3. **The live set.** `araba.entityTags().add(HURDA_ETIKET)`: in Minecraft 26.2 `Entity.entityTags()` returns the
   entity's own `tags` set, so adding to it is `addTag` without the size limit. Only `.contains` on it was read.
4. **A built name.** `at.addTag(a.tag + "_at")` adds `olum_at` when `a.tag` is `"olum"`. It cannot be resolved from
   the text, and it was skipped without a word, so the checked `olum_at` read as never added.

### 43.2 Decisions

- **A constant is the one Java binds** (`_Consts`). Each `static final String` belongs to the innermost class that
  declares it (classes and their bodies are found on a copy of the text whose literals and comments are blanked).
  A use reads, in order: the enclosing classes, innermost first; for `Owner.NAME` (or `pkg.Owner.NAME`) the classes
  named `Owner` when they agree; a static import (`import static pkg.Owner.NAME;` or `.*`); another class of the same
  file; and last a name only one value is declared under anywhere. A constant may be built from others
  (`BASE + "hurda"`); one built from itself stays unknown. `datapack_java` still gets one table by name (the names
  whose declarations agree), now including constants built from others.
- **What a String expression can be** (`_values`): a literal or a constant is one name; `c ? A : B` both (nested
  conditionals and a `?` or `:` inside a literal handled); a concatenation of known parts a name (with a small
  conditional inside, each combination, up to 16); a concatenation with an unknown part a **pattern**
  (`a.tag + "_at"` -> `*_at`, `PRE + i + "_" + j` -> `pre_*_*`); anything else the pattern `*`.
- **The live set counts**: `entityTags()` / `getTags()` / `getScoreboardTags()` / `getCommandTags()` followed by
  `.add(...)` is an add, `.remove(...)` a remove, `.contains(...)` a check, and a project helper whose body does
  one of them with its String parameter is a tag helper like one that calls `addTag`. The argument is read to its
  closing parenthesis, so `addTag(name.toLowerCase())` and `addTag(tagOf(e))` are read (as `*`) instead of not
  matched; a call spelled in a comment or a string is not one.
- **A built name is a lead, never an add.** The adds whose name is a pattern are listed once in the summary
  (`tag names Java builds at run time (N): Atlilar.java:751 adds *_at`) and in the JSON (`tags_added_dynamically`,
  `{at, pattern}`). A checked tag no add spells stays in "tags checked but never added"; when a pattern fits it the
  row says so (`olum_at  Atlilar.java:1668  (maybe added by Atlilar.java:751: *_at)`, JSON `maybe_added_by`), so
  "never added" (nothing could add it) and "no add spells it, but this line may" are two answers. A pattern of `*`
  fits every tag and marks none; it is listed in the summary as "adds a name the text does not spell". A method
  that hands its own String parameter to `addTag` is not one: its callers name the tag (and are read through the
  helper). `datapack tag NAME` lists the fitting built adds after the sites (`maybe  java  Atlilar.java:751 adds
  *_at (a name built at run time)`), also when no site names the tag.
- **Cost.** Only the files that declare a `static final String` or call the tag API (or a tag helper) are blanked
  and read for classes; the tag helpers are looked for only in files that call the tag API; a file's line starts
  come from one regex instead of a loop over its characters.

### 43.3 Measured

- **Fixture of the issue** (`tests/test_datapack.py`): the four shapes and the hand-set tag in five classes, two of
  them declaring `ETIKET` with different values. Before: "tags checked but never added (5)"; after: 2, `olum_at`
  with `maybe added by Atlilar.java:5: *_at` and `musallat_korumali` with nothing; `datapack tag croat` lists
  `Croatoan.java:7` as the add and `LuciferTeklifi.java:5` (`Croatoan.ETIKET`) as the check.
- **The example mods** (`examples/forge_mod`, `examples/glow_mod`): the summary is unchanged, line for line.
- **A synthetic mod** of 430 Java files (5.4 MB; 86 of them declare their own `ETIKET`, add it directly and through
  a conditional, check it and add a built name): before, 0 tags (every `ETIKET` dropped); after, 172 tags, 0
  "never added", 86 built adds. `index` + `problems`, median of five: 1.48 s -> 1.32 s (the line-start and helper
  changes pay for the new reading).
- The private mod of the issue is not on this machine: its acceptance (croat, musallat_koylu and yikim_hurda leave
  the list; olum_at stays with the note; musallat_korumali stays) is shown on the fixture that copies its shapes,
  not on the mod.

### 43.4 Not done

A tag held in a local or a field (`String t = c ? A : B; e.addTag(t)`) is `*`; a constant inherited from a
superclass or an interface that another class also declares with another value is not bound (the superclass is not
followed); a name built with `String.format` / `.formatted` / a `StringBuilder` is `*`, not a pattern; a check whose
name is built (`contains(a.tag + "_at")`) is not a check of any tag; mcfunction macros (`tag @s add $(x)_at`) are
still listed apart without a pattern; Kotlin.

## 44. Java calls into datapack functions in the graph (D71, 2026-09-29)

### 44.1 Why

D69 made `verinoda datapack function` list a function's Java callers, but read them at query time only: the graph
had no edge from Java into a function, so `when ns:x` (when does it run?), `trace`, impact, the MCP `analyze` and
`node_inspect` still saw the mcfunction callers alone (42.4, first item). In a mod the answer to "when does this
function run?" is usually a Java path: an event handler, a command, a weapon. On the forge example `when
emberforge:debug/reset_forges` answered nothing useful, and for a second reason: a function id with a folder
(`ns:dir/name`, the common layout) was read as a file path (`no file named ...; nearest: emberforge:debug/
reset_forges`), so no function in a folder could be named at all.

### 44.2 Decisions

- **`datapack_java.graph_edges(g)`** runs D69's `scan` over the graph's Java files (only when the graph has an
  `.mcfunction` node) and turns every call bound to one function into an edge from the innermost Java method (else
  class) whose lines hold the call to the function's node: `calls`, or `registers` with the delay in ticks for a
  `schedule function` command string (as an mcfunction `schedule` is, D52). `INFERRED`, `_origin =
  verinoda.datapack_java`, `context` says how (`helper ExampleMod.runFunction demo:alpha`, `command string
  (schedule) demo:later`, `function lookup ...`). A name built at run time is no edge (it names no one function),
  and neither is a call in a reference tree (not the running project). One edge per method, function and relation;
  when a method both looks the function up and runs it (`if (get(id).isEmpty()) return false; perform("function
  ...")`), the edge is at the run, not at the existence check, so `when` does not show the check's condition as
  the call's.
- **Stored with the other derived JVM edges**: the receiver sidecar keeps them under `datapack` (version 9: an
  older sidecar is recomputed once on the next load); `augment_python_receiver_calls` adds them when there is no
  sidecar. A method's lines are asked only for the files that hold a bound call (a span parses the file).
- **A function id names the function.** `naming.exact_nodes` reads `ns:a/b` (lowercase, a colon, at least one
  `/`) as a datapack function id before any other lookup: the `.mcfunction` node labelled with it. An id without a
  folder (`wings:fold`) was already found by its label.

### 44.3 Measured

- **forge example** (`examples/forge_mod`): before, `when emberforge:debug/reset_forges` was `not_found`; after,
  one path: `ModEvents.onRegisterCommands` -> `ForgeCommands.register` (Kotlin) -> `ForgeFunctions.resetForges` ->
  the function at `ForgeFunctions.java:23` (the `performPrefixedCommand`, not the `isEmpty()` check at line 19).
- **Fixture** (`tests/test_datapack_java.py`): a helper call, a scheduled command string (`2s` -> 40 ticks) and a
  check-then-run method give exactly three edges; `when demo:alpha` reaches `fire` and its caller `onHit`.
- **Cost**, on a synthetic mod of 430 Java files (5.4 MB) with 20 functions and 43 calling files: 43 edges;
  `graph_edges` 1.6 s cold (three fresh processes), paid when the sidecar is refreshed after an update, not on each
  load. A first version asked every method's lines (every Java file parsed): 12.5 s under the profiler, 2.4 s
  after asking only the calling files.

### 44.4 Not done

Kotlin callers (D69 reads Java only); a Java call to a function tag (`#ns:tag`) is not an edge to its members;
advancement rewards and predicates that run a function are not callers; the edge is to the first node of a function
a datapack copied into two places defines.

## 45. Translation keys in Minecraft lang files (D72, 2026-09-30)

### 45.1 Why

A mod's text is one lang file per locale (`assets/<ns>/lang/<locale>.json`, `.lang` before 1.13), `en_us` the one
the others are translated from. Nothing checks that they agree: a key added to `en_us` and never translated shows
English in another language, a key written twice keeps only its last value, a `%s` dropped from a translation
loses its argument, and a key the code asks for that no file defines shows the raw key in game.

### 45.2 Decisions

- **`verinoda/langkeys.py`**, next to `shaders.py` and `datapack.py`: text only, no graph and no index needed.
  Lang files are found by path (`.../assets/<ns>/lang/<locale>.json|.lang`, the same skipped build folders as the
  shader reader) and grouped by namespace; each namespace's default locale (`en_us`, `--default` for another)
  is compared with its other locales. As in game, every file of one locale in a namespace is merged: a
  multi-loader layout (`common/` and `fabric/` each with an `en_us.json`) or datagen output beside a hand-written
  file is one default locale, and a key is cited at the first file (by path) that defines it. A locale with
  several files is merged the same way before keys are counted missing from it.
- **A JSON lang file is read key by key** (`json.decoder.scanstring` and `raw_decode`), not with `json.loads`, so
  every key keeps its line and a key written twice keeps both lines; lines come from the offsets of the file's
  newlines (bisected), so a large file reads in linear time. A file that is not a flat JSON object is an
  `invalid_file` finding with the line where reading stopped, and is not compared (no flood of "missing" keys).
  As in game, a value that is an object, an array or null fails the whole file (`GsonHelper.convertToString`),
  a number or boolean is kept as its text, and a value nested too deeply to decode is `invalid_file` rather than
  a crash. A `.lang` file's UTF-8 BOM is dropped, as the JSON reader's is.
- **Findings**, each `{kind, key, status, at, other_at, why}`; `at` and `other_at` are the two files the finding
  rests on (the "done when"):
  - `missing_in_locale`: the default file's line, the other locale's file;
  - `extra_in_locale`: the other locale's line, the default file;
  - `placeholder_mismatch`: the other locale's line, the default file's line. Placeholders are compared as
    `{argument index: conversion}`, so `%s %s` equals `%2$s %1$s` (a translation may reorder) and `%%` is not a
    placeholder. Every `%d` and `%f`, with a width or precision (`%.1f`, `%5d`, `%2$d`), is read as `%s`, as
    the game rewrites them when it loads a lang file (`%(\d+\$)?[\d.]*[df]`);
  - `duplicate_key`: the line that wins and the first one (both in the same file);
  - `missing_key`: the call site (`Component.translatable`, `Text.translatable`, `new TranslatableText`,
    `new TranslationTextComponent`, `I18n.get/format`, the 1.12 `I18n`/`StatCollector` forms, with a literal
    key) and the default file of the namespace a dot segment of the key names. The literal must be the whole
    argument: `"tooltip.gem.level." + n` and Kotlin's `"tooltip.gem.level.$n"` are built at run time and are not
    candidates. Comments are blanked (newlines kept) before the calls are searched, so a commented-out call asks
    for nothing. A key with no segment naming a namespace of the project (`gui.done`) is vanilla's or another
    mod's and is not reported; `minecraft` never counts as a project namespace here (an `assets/minecraft/lang`
    override repeats only the vanilla keys it changes);
  - `unused_key`: the default file's line; `other_at` is empty and `why` says how many code and resource files
    were searched (a search that found nothing has no second file to cite).
- **Status**: every comparison of two files and a literal key no file defines is `statically_verified` (read
  from the lines cited). `unused_key` is `strong_inference`: a key counts as used when a string literal names it
  in full (Java, Kotlin, advancement and other resource JSON, mcfunction), when a literal prefix ending in `.` or
  `_` with at least two segments starts it (`"tooltip.gem.level." + n`), or the head of a Kotlin or Groovy
  template does (`"tooltip.gem.level.$n"`), or when it is `<registry kind>.<ns>.<path>` and `path` / `ns:path`
  is a literal or a resource file stem (the game builds `item.gem.ruby` from the registered id `ruby`; a
  folder id `tools/ruby_pick` builds `item.gem.tools.ruby_pick`), `<kind>.<id>` with `id` a literal
  (`itemGroup.gem`), or the 1.12 `tile|item|entity|fluid.<name>.name` with `name` a literal
  (`setTranslationKey("gem.ruby_ore")`). Single-quoted strings of `.js`, `.ts` and `.groovy` files (KubeJS)
  are literals too. Keys of an `assets/minecraft/lang` file override vanilla text and are never unused.
- **`verinoda lang [--default en_us] [--no-unused] [--json]`**: counts per kind, then one line per finding
  `kind [status] at <- other_at - why`, in file and line order (lines compared as numbers). Exit 3 when
  something is found (as `shader --check`); 2 when nothing was compared: the project has no lang file
  (`status: no_lang`) or no namespace has the default locale (`status: no_default`, a typo in `--default`), so a
  CI gate does not pass on a check that did not run; 0 otherwise.
- **No MCP tool.** The other Minecraft validators (`datapack`, `shader`, `trace-log`) are CLI only, and `run_tool`
  reaches the core tools; a `lang_check` tool for the full profile can follow if agents ask for it.

### 45.3 Measured

Verinoda's own repository (the two example mods, 4 lang files, 330 files searched): nothing disagrees.

### 45.4 Not done

- Keys are grouped by namespace across the whole repository: two mods of one repository with the same namespace
  are compared as one (right for a multi-loader mod, wrong for two unrelated mods sharing an id). Which of two
  default files wins for a key defined in both is not known (pack order); the first by path is cited.
- Only literal keys in the listed translate calls are `missing_key` candidates; a key passed through a constant,
  a helper or a data generator's `add(...)` call is not followed. Datagen output is read only when it is in the
  tree.
- A key built at run time any other way than a literal prefix, a template head or a registered id is reported
  as unused (hence `strong_inference`). Literals are read from comments too, so a key named only in a
  commented-out line counts as used. A JavaScript template literal (backticks) is not read.
- Comment blanking does not know Java text blocks or Kotlin raw strings (triple quotes); a `//` inside one could
  hide a call on the same line.
- `%,d` and other flags the game does not rewrite are not placeholders here.
- Findings are printed, not stored as claims in the store; the text formatting codes (`§`) and Minecraft's
  `%s` count against an actual `Component.translatable` argument count are not checked.

### 45.5 Tests

`tests/test_langkeys.py`:

- `test_the_lang_file_reader_keeps_every_line_of_a_key_written_twice`
- `test_placeholders_are_argument_indexes_with_their_conversion`
- `test_each_locale_finding_cites_both_files` (missing, extra, placeholder mismatch, reordered positional
  arguments not reported, duplicate)
- `test_the_code_against_the_default_file` (missing key at its call site, vanilla key skipped, unused only when
  no literal, prefix or registered id reaches the key)
- `test_a_clean_pack_and_a_broken_file` (invalid JSON reported with its line, not compared)
- `test_legacy_lang_files_and_another_default` (`.lang`, `--default`)
- `test_cli` (exit codes 3 and 2, `--json`, `--no-unused`)
- `test_a_key_built_at_run_time_is_not_a_missing_key` (Java concatenation, Kotlin template)
- `test_placeholders_follow_what_the_game_loads`, `test_numeric_placeholders_compare_as_the_game_sees_them`
- `test_every_default_file_of_a_namespace_is_the_default_locale` (multi-loader `en_us` files merged)
- `test_a_vanilla_override_does_not_make_vanilla_keys_missing`
- `test_how_else_a_key_is_named` (KubeJS single quotes, 1.12 `tile.*.name`, `itemGroup.<id>`, folder ids)
- `test_findings_follow_the_file_line_by_line`
- `test_no_file_of_the_default_locale_is_not_a_clean_pass` (`no_default`, exit 2)
- `test_a_value_the_game_cannot_read_fails_the_file` (object value, deep nesting)
- `test_a_large_file_reads_in_linear_time` (20,000 keys)
- `test_a_bom_in_a_legacy_file_is_not_part_of_its_first_key`
- `test_a_commented_out_call_asks_for_nothing`

Also touched: `README.md` (a `lang` row in the Commands table) and `docs/ARCHITECTURE.md` (a `langkeys.py` row),
both required by `tests/test_docs.py`.

## 46. Extract the definition around a location (D73, 2026-09-30)

### 46.1 Why

An agent or a user who has a location (a traceback line, a compiler error, a `file:line` from a review) wants the
unit of code around it, whole: the function, else the class. `node_inspect` needs an index and a name and cuts the
source at 30 lines; `query` ranks passages; reading a file by hand means guessing where the function starts and
ends. The definitions with their exact spans already exist: `anchors.py` (D23-D25) computes them per file, for
Python with its own parser and for the other languages through tree-sitter, and `anchors.enclosing()` already
answers "the innermost symbol around these lines" for evidence anchors.

### 46.2 Decisions

- **`verinoda extract TARGET... [--from FILE|-] [--max-lines N] [--limit N] [--no-numbers] [--json]`**, a new
  module `verinoda/extract.py` and a thin `cmd_extract` in `cli.py`, as `backlog`, `datapack` and `shader` are.
- **Targets**: `path:LINE`, `path:LINE-LINE` (the innermost definition holding both ends; a reversed range is
  read low to high), `path:LINE:COL` (the column is ignored, as a compiler prints it), `path#Symbol` or
  `path::Symbol` (`anchors.symbols_named`: `m` finds `C.m`; several matches are `ambiguous` with their lines). A
  pytest node id works as it is pasted: `tests/test_x.py::TestA::test_b` is `TestA.test_b`, and a parametrised
  id's `[1-2]` is dropped. Any other argument is read as a line of tool output.
- **Tool output** (`--from FILE`, `-` for stdin; also a pasted error line as the argument): each line is tried
  against, in order, a Python traceback line (`File "x", line N`), a JVM frame (`at a.b.C$D.m(C.java:N)`, found by
  its package path `a/b/C.java`), Maven's `path:[N,C]`, tsc/MSBuild's `path(N,C)` and the common `path:N[:C]`
  (gcc, javac, rustc, pytest, eslint, ruff). On each line the pattern whose first match starts leftmost wins (ties
  in that order), so a call in the message after a location (`src/a.py:2: assert v.get(3) == 4`) does not hide
  it; a location repeated is read once. Maven's and MSBuild's paths start only after a separator and are at most
  255 characters, so a long line without spaces (minified code) is scanned in linear time. This is a line
  scanner, not `failsig.py`'s per-test failure parsers: compiler diagnostics have no test.
- **Paths**: as given (relative to the working directory, then to the project root); else, for a relative path,
  the one project file (`snapshot.listed_files`) whose path ends with it (javac run in a module prints paths
  relative to that module; a bare `orders.py`). An absolute path outside the project is never guessed from its
  name (`/usr/lib/python3/json/decoder.py` must not become the project's `decoder.py`); a POSIX absolute path counts
  as absolute on Windows too. `.git` and `.verinoda` are never read. The output patterns stop at a space, so for a
  path that follows a space the absolute paths starting earlier on the line (a drive, a root, after a space, a
  quote or MSBuild's `1>`) are tried first: `C:\Users\A B\src\a.py:3` is found. They are used only when such a
  file exists inside the project. The project's file list is read once per run, when the first suffix match needs
  it.
- **What comes out**: the innermost function or class (`kind` `def` / `class`, qualified `name`, `start`-`end`
  with decorators, `inside`: the enclosing classes), whole; a top-level statement when the line is in no definition
  (`LIMIT = {...}` over several lines, named by what it binds); a Markdown section for a document (D25's sections).
  A line between definitions is `no_definition`; a language without a parser (`.glsl`, `.mcfunction`) or a file
  that does not parse is `unsupported`; statuses per location: `found`, `not_found`, `ambiguous`,
  `no_definition`, `unsupported`.
- **No index.** The file on disk is parsed now (`anchors.facts_for_path`, memoised by content hash): the answer is
  never stale and works in a project never scanned. Cost on this repository: about 1.5 s per call, almost all of
  it Python start-up and imports.
- **Claims.** Each definition found is a claim, returned, not stored (a read, as `backlog` is): "`src/a.py:40` is
  inside function `C.m` (`src/a.py:35-52`)" or "`C.m` is a function at ...", with the definition's lines as
  evidence (`evidence.source_evidence`: locator, content hash, HEAD commit). `statically_verified` from a clean
  parse; `strong_inference` with an uncertainty when tree-sitter reported syntax errors and recovered (the span it
  recovered may be wrong). Nothing is a heuristic except the path-suffix match, which only chooses a file when
  exactly one file matches.
- **Output locations outside the project** (library and JDK frames, generated files, `example.com:80`) are counted
  in `not_in_project` with the first five, not listed as failures; an explicit target that is not found is always
  reported. `--limit` (20) caps the locations of the project read; the rest are counted (`locations_left`), not
  read. Locations outside the project are not capped (a project frame after 30 JDK frames must still be found);
  each costs a path check and a suffix match over the file list read once.
- **The printed lines** are split as the parsers count lines (`\n`, `\r\n`, `\r`), not with `str.splitlines`,
  which also ends a line at a form feed and would shift the source from the span the claim states. A negative
  `--max-lines` is a usage error.
- **Exit codes**: 0 when every location was found, 2 otherwise (as `backlog`, `when`); a call with no target and
  no `--from` is a usage error.
- **No MCP tool in this change.** The nearest commands (`backlog`, `datapack`, `shader`, `trace-log`) are CLI-only,
  and a new tool changes the tool count that README, ARCHITECTURE and UPGRADING state (checked by
  `tests/test_docs.py`; UPGRADING is the integrator's). The natural exposure is `code_extract {target}` in the full
  profile or behind `run_tool` next to `node_inspect` (read-only; one `extract.run` call). See open issues.

### 46.3 Not done

- Innermost definition only; a whole class around a method is one more call (`path#Class`, or the `inside` list).
- Languages: Python, Markdown and the tree-sitter grammars in `anchors.TS_LANGS` (JS/TS, Java, Kotlin, Go, Rust,
  C/C++, C#, Ruby, PHP, Scala, Swift, Lua). A Kotlin or Scala top-level function, a Go method and so on are found
  as far as `anchors.TS_DEF_TYPES` knows the node type; a Python file that does not parse has no fallback.
- The output scanner is line-based: a location split over two lines, or printed only as a module or class name
  (a Python `in f` without a file, a Go panic's package path) is not found. CI logs with absolute paths of another
  machine are counted as outside the project (never guessed).
- A JVM frame names its file, not its folder: a class in a source set whose folder does not follow the package
  (`src/main/kotlin/Foo.kt` in package `a.b`) is not found from the frame.
- No MCP exposure yet (above).
- The evidence's content hash is computed by `evidence.source_evidence` over `str.splitlines` lines (the
  repository-wide convention); in a file with a form feed or another such separator the hashed lines can differ
  from the printed ones. A lone `\r` ends a line for Python's parser but not for tree-sitter's; the printed lines
  follow Python's.
- A wider absolute path with spaces is tried only when the path a pattern read follows a space and an absolute
  path starts earlier on the line; a relative path with spaces (`my dir/a.py:3`) is not found.

### 46.4 Tests

`tests/test_extract.py`:

- `test_a_line_gives_the_innermost_enclosing_function_whole`: decorator included, nested helper inside, `inside`,
  claim text, status and evidence.
- `test_nested_definition_class_and_range`: nested function, class, a range over two methods, a column ignored.
- `test_top_level_statement_blank_line_and_past_the_end`.
- `test_a_symbol_by_name`: `#m`, `::C.m`, ambiguous, missing.
- `test_java_method_and_inner_class`.
- `test_a_path_from_another_directory_is_found_by_its_end`.
- `test_a_parse_the_tree_recovered_from_is_strong_inference` (TypeScript with a syntax error).
- `test_markdown_section_and_unsupported_language`.
- `test_locations_in_compiler_and_test_output`: traceback, `path:N:C`, `path(N,C)`, Maven, JVM frame, a repeat, a
  time and a URL that are not locations.
- `test_output_locations_outside_the_project_are_counted_not_listed`, with `--limit`.
- `test_parse_target_forms`, `test_cli_prints_the_definition_with_line_numbers`,
  `test_cli_json_from_a_file_and_exit_codes`.
- `test_a_call_in_the_message_does_not_hide_the_location` (`v.get(3)` after `path:N`).
- `test_a_long_line_without_spaces_is_scanned_in_linear_time`.
- `test_an_absolute_path_with_a_space` (the gcc form and MSBuild's `1>` form).
- `test_frames_outside_the_project_list_the_files_once`.
- `test_negative_max_lines_is_refused_and_a_reversed_range_is_kept`.
- `test_a_form_feed_does_not_shift_the_printed_lines`.
- `test_a_pytest_node_id` (nested `::` and a parametrised id).

Run: `python -m pytest tests/test_extract.py tests/test_docs.py tests/test_cli.py tests/test_anchors.py`.

## 47. Dependency cycles and a minimal break set (D74, 2026-09-30)

### 47.1 Why

`map --view dependencies` lists the heaviest file-to-file dependencies but never says which of them go round in a
circle, and the upstream `find_import_cycles` (project_index/analyze.py) is not reachable from any command, lists
short cycles one by one (a large knot shows as dozens of overlapping rings) and does not say what to cut. The tools
people compare against (Madge `--circular`, dependency-cruiser `no-circular`, Sonargraph's cycle groups and "minimal
set of dependencies to cut") answer two questions: which files are tangled together, and which dependencies to
remove to untangle them.

### 47.2 Decisions

- **A new map view, `cycles`**, beside the other seven: `verinoda map --view cycles [--json]`, part of the all-views
  `map`, and `map_view {view: "cycles"}` on MCP (`map_view` is behind `run_tool` in the core profile, so the core
  profile stays at five tools). No new command, no new flag.
- **What a dependency is**: the dependencies view's edges (`calls`, `imports`, `imports_from`, `uses`, `inherits`)
  between two different files, aggregated file to file. A type-only import (`import type`, stamped `type_only`) and
  a deferred `import(...)` (`deferred`) are left out, as upstream's import-cycle finder already does: neither closes
  a cycle when the code loads. Prose files are never code. Calls count, not only imports: two Java classes of one
  package call each other without any import, and that is the tangle Sonargraph reports; each dependency lists its
  relations so an import cycle can be told from a call cycle.
- **A cycle is a strongly connected component** of that file graph (two or more files), not an enumeration of
  simple cycles: one row per tangle, however many rings it holds.
- **The break set** of a cycle is the fewest file-to-file dependencies whose removal leaves its files acyclic; among
  sets of that size, the one with the fewest references behind them (the cheapest to cut). Minimum feedback arc set
  is NP-hard, so:
  - up to 12 files (`EXACT_BREAK_MAX`): exact, by dynamic programming over subsets of files (each ordering's
    backward edges are a break set; the cheapest ordering gives the minimum), `break_method: "exact"`;
  - larger: the Eades-Lin-Smyth ordering, then every cut dependency that closes no cycle with what is kept is put
    back (heaviest first), `break_method: "greedy"`: an upper bound in which no single cut is unneeded.
- **Evidence**: each dependency carries its reference lines (`at`, `file:line`, EXTRACTED ones first). Each cut
  names a cycle it closes (`closes`: the cut, then the shortest way back, preferring EXTRACTED edges) with one
  reference line per step (`steps`), so the whole ring can be read in the code.
- **Status**: graph edges are extractions, never verification, so a cycle is `strong_inference` at most: when the
  parser's own (EXTRACTED) edges alone strongly connect all of its files; otherwise `weak_inference` with a `note`
  (some link rests on an INFERRED edge only: a receiver's type, a name). Each cut's `closes_status` says the same of
  the ring it names.
- **Detected copies** (the detection `dataflow` already uses) are kept apart from the project: a copy is, by its
  detection, hardly used from outside its folder, so a dependency between a copy and the project is a name resolved
  into the wrong tree; it is left out (listed in `left_out` with its lines), so a copy's cycles never merge with
  the project's; they are listed after the project's, marked `in`. **Configured reference trees**
  (`setup --reference`) may be vendored code the project really loads: their dependencies stay, a cycle wholly
  inside one is marked `in`, and one that crosses into the project is the project's.
- **A standard library import is no dependency**: the graph can resolve `import html` in
  `pkg/security.py` to `pkg/exporters/html.py`. A Python `imports`/`imports_from` edge whose source line imports
  only standard library modules (absolute, `sys.stdlib_module_names`, and no top-level name of the project, from
  the root or `src/`, shadows it) is left out and listed in `left_out`. On Verinoda's own repository that one edge
  had joined `paths <-> security` and `build -> dedup -> llm` into one 13-file `strong_inference` cycle.
- **Every list is capped** (50 cycles, 100 files, 60 cuts, 60 dependencies per cycle, 20 steps per ring, 20
  left-out dependencies), with `truncated: true` when one was cut and `cycles_total`, `size`, `break_set_total`,
  `dependencies_total`, `closes_steps_total`, `left_out_total` for the full counts. The greedy put-back shares a
  budget of 2,000,000 visited edges; past it the cuts not yet tried stay cut (still a break set),
  `break_method: "greedy, not reduced"`, and a limit says so. Dependencies are bucketed into their cycles in one
  pass and the Eades-Lin-Smyth ordering uses a heap.
  On Verinoda's own repository a first version reported one 46-file tangle of `verinoda/` and
  `benchmarks/corpora/heldout_repoatlas_*` files together; with the 10 crossing dependencies left out it is the
  project's 41 files, and the copy's cycle is its own row.
- **Order**: the project's cycles first, largest first, then by file name.
- **Text**: the plain-text summary stays within its line budget (a cut takes two lines) and always ends with what
  it left out (`... N more cuts in --json`, `... N more cycles in --json`). A cut resting on INFERRED edges only is
  marked `[INFERRED edges only]`, and a ring it closes that only INFERRED edges complete `(weak_inference)`. The
  break set's cost still treats such a dependency like an extracted one (a limit says so): whether the edge exists
  is unknown, so neither preferring nor avoiding it is justified.

### 47.3 Measured

- `examples/orders_app`: no cycles among its 7 files with dependencies.
- Verinoda's own repository (existing index): 14 cycles over 94 files, 36 cuts, exact for 13 of the 14 (before the
  standard library imports were left out: 13 cycles over 102 files, the false 13-file cycle among them); 4 import
  dependencies left out as standard library, 10 as crossing into the detected copy.
- Synthetic, one strongly connected set of 1,000 files and 9,000 dependencies: 1.6 s, 39 KB of JSON (was 6.6 s,
  1.46 MB); 3,000 files and 27,000 dependencies: 2.8 s, 42 KB (was 63 s, 4.8 MB); 3,000 two-file cycles and
  60,000 acyclic dependencies: 2.8 s, 44 KB (was 33 s, 2.6 MB).
  The largest is 41 files of `verinoda/` (`weak_inference`: some links are INFERRED only); several of the cuts it
  proposes are already function-level (lazy) imports (`naming.py:233 -> question_plan.py`,
  `codecheck_rank.py:354 -> codecheck.py`, `datapack.py:445 -> datapack_java.py`): the view counts them (see
  Limits), and they show where the code already works around a cycle.

### 47.4 Not done

- File level only; package-level cycles (Sonargraph's second level) are not computed.
- An import inside a function body counts like one at the top of the file: the graph does not record where an
  import sits, so a lazy import (Python's usual way out of a cycle) still shows as a dependency.
- Dynamic dispatch, reflection and DI are not resolved (the dependencies view's limits).
- The break set says what to cut, not how (move, invert, inject); it minimises the count of file dependencies, not
  the work of removing them (the reference count is only the tie-breaker).
- Greedy sets for cycles over 12 files are not proven minimal; past the work budget they are not even reduced.
- The standard library check reads the import line: an import spread over several lines, or a stdlib name under a
  project package that is also importable at the top level some other way, is not recognised.
- No CI mode (exit 1 on a new cycle); that belongs with the architecture rules engine (section 7 of the backlog).

### 47.5 Tests

- `tests/test_cycles.py`
  - `test_exact_break_set_is_the_smallest_by_edges_then_references`: against brute force on random small graphs.
  - `test_two_files_that_depend_on_each_other_cut_the_lighter_dependency`
  - `test_greedy_break_set_leaves_no_cycle_and_no_cut_that_could_be_put_back`: and never smaller than the exact set.
  - `test_type_only_deferred_and_prose_edges_close_no_cycle`
  - `test_a_cycle_that_only_inferred_edges_close_is_weak_inference`
  - `test_a_copy_of_the_project_is_kept_apart_and_listed_last`: and the crossing dependencies are in `left_out`.
  - `test_a_configured_reference_tree_keeps_its_dependencies_on_the_project`
  - `test_a_standard_library_import_resolved_to_a_project_module_closes_no_cycle`
  - `test_large_inputs_are_capped_and_marked_truncated`: 55 two-file cycles and a 150-file knot with a small work
    budget; every list capped, `truncated`, `greedy, not reduced` still a break set.
  - `test_cycles_text_stays_in_its_lines_and_says_what_it_left_out`
  - `test_cycles_text_marks_a_cut_on_inferred_edges_only`
  - `test_scanned_cycles_with_their_break_set_and_evidence`: a scanned Python project with a 3-file and a 2-file
    cycle; each cut's ring and its `file:line` steps.
  - `test_cycles_text_names_the_cut_and_the_cycle_it_closes`
- `tests/test_architecture_map.py` (the view set), `tests/test_cli.py` (`map --view cycles --json` and text),
  `tests/test_mcp.py` (`map_view("cycles")` equals the core view).

## Sources

- **Retrieval:**
  - Aider repomap: https://aider.chat/2023/10/22/repomap.html
  - Agentless: https://arxiv.org/abs/2407.01489
  - AutoCodeRover: https://arxiv.org/html/2404.05427
  - SWE-agent: https://arxiv.org/html/2405.15793
  - LocAgent: https://arxiv.org/html/2503.09089
  - CodeRAG-Bench: https://arxiv.org/abs/2406.14497
- **Verification:**
  - Chain-of-Verification: https://arxiv.org/abs/2309.11495
  - CRITIC: https://arxiv.org/abs/2305.11738
  - Self-RAG: https://arxiv.org/abs/2310.11511
  - FEVER: https://arxiv.org/abs/1803.05355
  - ALCE: https://arxiv.org/abs/2305.14627
  - Build Systems à la Carte: https://doi.org/10.1145/3236774
  - Salsa: https://github.com/salsa-rs/salsa
- **Runtime:**
  - PEP 669: https://peps.python.org/pep-0669/
  - coverage.py contexts: https://coverage.readthedocs.io/en/latest/contexts.html
  - jedi: https://jedi.readthedocs.io
  - SCIP: https://github.com/sourcegraph/scip
- **Question understanding:**
  - AmbigQA: https://arxiv.org/abs/2004.10645
  - ClarifyGPT: https://arxiv.org/abs/2310.10996
  - HyDE: https://arxiv.org/abs/2212.10496
  - Least-to-Most: https://arxiv.org/abs/2205.10625
  - Can et al. 2008 (Turkish IR): https://doi.org/10.1002/asi.20750
- **References:**
  - PEP 740: https://peps.python.org/pep-0740/
  - deps.dev: https://docs.deps.dev/api/
  - arXiv API: https://info.arxiv.org/help/api/
  - Software Heritage: https://docs.softwareheritage.org
  - GitHub REST: https://docs.github.com/rest
