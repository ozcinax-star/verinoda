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
| D31 | No laundering (truth rules) | implemented | Python for relation scopes, config bindings, order and location existence; other languages keep their grades. Word overlap alone never verifies; `contains:` verifies only the quoted text; written claims naming more than the typed check binds stay partial. In-sample fixtures; no held-out false-sentence set yet. |
| D32 | Name-existence check | partial | Python only: `verinoda check` (files, `--diff`, `--stdin --as`) and `verinoda api`, MCP `code_check` / `api_members`, skill text. Not done: mod config keys and resource ids, JVM jars, JS/TS, `--against PKG==VER`, the environment fingerprint in snapshots. Measurements in BENCHMARKS.md (the fixture set was written by the rule author: in-sample). |
| D33 | Decisions stay human | partial | Built: the `decide` intent (EN/TR cue tables) with the verdict `human_decision_required`, never `met`; decision records (`verinoda/decisions.py`, schema v5 log, `decide record/import/guard/accept/waive/list`, MCP `decision_record`); guards and `decide check` (`verinoda/guards.py`, MCP `decision_check`, a one-line summary in `update`; critique's exclusivity check and feedback's exclusive corrections use the same engine). Guard mutations (54 cases on the three examples, written by the rule author, plus 22 forms from the two reviews added by the fixer: all in-sample): VIOLATED precision 1.00, recall 1.00 in reach, 13/13 out-of-reach forms named in limits or POSSIBLE; the old raw-regex scan on the same orders_app cases tp 7 fp 8 fn 6. `decide check` median 42 ms per example case, about 2 s (1.8-2.2 s) on the full Verinoda tree (3 guards; results in `benchmarks/results/decide-2026-09-25/`). The decision brief (`verinoda/decision_brief.py`, `decide brief/answer`, MCP `decision_brief`, answers through `decision_record(action='answer')`; `analyze` routes decide sub-questions to it): on orders_app, EN and TR question, 8/8 gold forces, 19/19 cited evidence re-checks, 5/5 gold question kinds - in-sample (the gold came with the design and the probes were written after it). Not built: `analyze` impact questions do not include violations; the UI shows no decision badge; claims for accepted guards (kinds `exclusive` / `layering`); an ADR's reasons are matched by a few phrasings only; `research.dependencies` itself still reads no Gradle/Maven (the guards and the brief read them). Intent routing: the only held-out set left (held-out 4, 20 questions by the fixer, hashed before the review fixes' cue rules were written): precision 0.83, recall 0.50 - the recall bar (0.85) is not met; every other set (written, held-out 1-3, the reviewers' 52) is in-sample now. A missed choice question can still be judged `met`; its words then get a note at most. |
| D34 | Debug ledger (loop detection, strategies) | partial | Built 2026-09-25 (section 7): `verinoda debug start/try/status/diff/close`, strategies `differential/bisect/rerun/observe`, MCP `debug_start` / `debug_attempt` / `debug_status` / `debug_strategy` / `experiment_run`, schema v6. debugloops_v1 (12 sessions written by the builder, gold fixed before the rules ran; in-sample after three fixes): definitive precision 11/11, loop recall 8/8, 0/4 controls stopped, top strategy 8/8. A review found 27 problems (25 distinct: false stops, false "passed", unverified bisect ends, git-safety gaps); all fixed with regression tests (section 7.5), the benchmark scores unchanged after the fixes. Not built: a real agent session with and without the protocol. `debug try` overhead is copy-bound on big trees (median 2.4-5.0 s on 2,341 files, depending on machine load). |

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
  name"), as for plain words.
  An analysis stores a claim with its own uncertainties; the question's
  reading (a weak link, an open clarification) is added when the claim is
  shown, so a claim reused by a later question does not carry it.

Limits: Python only for relation scopes, config bindings and order (other
languages keep their partial grades). Calls through other names, dynamic
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
  `guarded`. A broad handler (bare `except`, `Exception`) guards an import,
  and another name only when it does not raise again (`except Exception:
  raise` handles nothing); a flag the module binds once to a constant
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
- Output: nearest real names (edit distance with transpositions, shared word
  parts, a few synonyms) and where the name is defined elsewhere. Wording:
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
  ask for a choice ("pick", "fits best") get a note, never another verdict.
  A `decide` sub-question's `done_when` kind is `decision_brief` and its
  verdict is always `human_decision_required` (`question_plan.HUMAN_DECISION`),
  never `met`, whatever claims exist. analyze runs no retrieval for it (the
  options a decision names need not exist in the code, so "these words occur
  nowhere" is not reported) and routes it to the decision handler.
- *Records.* A decision is a Markdown file with a front matter
  (`verinoda-decision: 1`, id, status, `decided-by: human`, supersedes,
  governs, guards, revisit-when, waivers) in `decisions.dir` (default
  `.verinoda/decisions/`, which git does not see; a committed folder for CI).
  The file is what is checked; every event (record, import, guard, accept,
  waive, supersede) also appends the whole state to the `decisions` table
  (schema v5, append-only), so a hand edit is visible. `decide record` needs
  the chosen option and the rationale in the human's words; MCP
  `decision_record` refuses record/guard/accept/waive without the user's words
  (`user_statement`). A hand-written ADR is never edited: `decide import` makes
  a companion record whose guards are only *proposed* (from sentences with
  only/must/never that name code the index knows) until the human accepts
  them. A record that says `decided-by` anything but `human` is reported and
  not enforced.
- *Guards* (`verinoda/guards.py`, one rule engine). `only_in` (a call may
  appear only in the allowed files; product code by default: tests,
  reference trees, detected copies and the decision folder are out of
  scope; `conftest.py` is test code): Python names are resolved by scope as
  Python does (module, function, lambda, class and comprehension scopes,
  `global`/`nonlocal`; a binding under if/for/while/try/except/match may not
  run, so the last unconditional binding and every conditional one after it
  can reach a use). A call is VIOLATED when every binding that can reach it
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
  VIOLATED, an INFERRED one POSSIBLE. `dependency absent|present` reads the
  root manifests plus the root Gradle/Maven build and the subprojects it
  includes (from the project's file list: git-ignored files are not read);
  build files under test, sample, fixture or vendor folders are skipped and
  named in the limits, another build is POSSIBLE; a Maven item is cited at
  its `<artifactId>` line, one finding per cited line. `governs` compares
  the symbol's anchor fingerprint: REVIEW, never VIOLATED. `revisit-when` fires TRIGGER once its
  condition starts to hold. Every `ok` states its scope and limits.
  `decide check` exits 1 on VIOLATED and 2 on an error; with `--base REF` /
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
  `--option`, or used by the project) carry presence evidence, the code a
  change touches, installed metadata (read from the project's venv, never
  imported) and external claims only as quote-checked pins: the page is
  fetched through `research` as `research.network` allows and the quote must
  occur verbatim, else it is dropped (`unknown` without the network); even
  then it says what the page says, not that it applies here. The agent's own
  arguments are `weak_inference`. `questions_for_human` come from fixed
  EN/TR templates per kind, at most 5, each with `asked_because` and
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
  the intent itself; the rules are the fallback.

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
    one by name (Python, `x.test.js` / `x.spec.ts`, `x_test.go`, `FooTest.java`) or the file a failing
    test of the session lives in. Adding a new test, only adding lines to one (a print, a comment), or
    a change of formatting alone (the file's syntax tree unchanged; an assertion line whose statement
    is the same after parsing) is not flagged. "An assertion or expected value" = a removed/changed
    line matching assertion forms (`assert`, `self.assert*`, `expect(`, `assertThat`, `assert_eq!`,
    `t.Errorf`, `pytest.raises`, `expected =`/`want :=`, ...). It stops even a passing attempt (a test
    edited until it passes is the case to ask about).
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
  changed since the last passing state, each with its evidence; only a complete trace in which a
  changed function was called nowhere, with no child process started, rules it out);
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
- **Schema v6** (v5 is reserved for the decisions branch): `debug_sessions` (symptom, command, base
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
