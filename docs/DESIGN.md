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
| D31 | Name-existence check | partial | Python only: `verinoda check` (files, `--diff`, `--stdin --as`) and `verinoda api`, MCP `code_check` / `api_members`, skill text. Not done: mod config keys and resource ids, JVM jars, JS/TS, `--against PKG==VER`, the environment fingerprint in snapshots. Measurements in BENCHMARKS.md (the fixture set was written by the rule author: in-sample). |

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

### 4.3 Name existence (added after round 3)

**D31. Name-existence check** (`verinoda check`, `verinoda api`; `codecheck.py`,
`codecheck_env.py`, `codecheck_facts.py`; needs the `precise` extra).

- Finding: AI-written code imports modules, calls functions, passes keyword
  arguments and reads dict keys that do not exist, or not in the installed
  version. `resolve-call` answered "unresolved" both for a missing name and for
  a receiver of unknown type, never checked imports or keywords, and resolved
  against Verinoda's own interpreter instead of the project's environment.
- Sites: imports and from-imports, attribute loads, keyword arguments, and
  constant keys read from the dict literals a function returns. `--diff`
  checks the sites on changed lines (plus new files); its revision is resolved
  to a commit first, so it is never read as a git option. `--stdin --as PATH`
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
  registry, `PATH`, a Python manager's directory, owned by root); `--env` is
  trusted as given. jedi imports a compiled module only if it is a
  standard-library module from the interpreter's own directories (a patch of
  `jedi.inference.imports._load_builtin_module` installed by `codecheck_env`,
  limited to the check's jedi project). The oracle imports no `X.__main__`.
- Closed-world rule: `absent` only from a closed container - a module with no
  `__getattr__`, `exec` or `globals()` writes and closed star imports; a class
  object; an instance made by a direct constructor call, or held by a single
  unreassigned local that is not handed to code that sets attributes (slotted
  instances are closed whatever they are handed to); one known signature
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
  under a platform or version condition (`os.fork` on Windows); `sys`
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
  returns a class). A decorator keeps a signature or class closed only when it
  comes from the module that defines it (`functools.cache`,
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
  `conftest.py` above the file that changes `sys.path`, or the module in a
  plain (non-package) directory of the project off the assumed search path
  (`lib/helpers.py`), makes a missing top-level module `unknown`, never "not
  found in this project"; a file inside a package does not count
  (`pkg/extractors/robot.py` is not `robot`).
- Constructors: a standard-library base with no `__init__`/`__new__` of its
  own (`abc.ABC`, a mixin) does not answer for a class's keywords; the next
  class in the MRO does (`class Plugin(abc.ABC, Base)` takes `Base`'s). When
  such a base's constructor comes from one of its own bases and another base
  follows it, the keywords are `unknown` (the real MRO may put that base
  first).
- Output: nearest real names (edit distance with transpositions, shared word
  parts, a few synonyms) and where the name is defined elsewhere. Wording:
  "not found in <container> as installed in <env> (<file>)", never "does not
  exist". Exit 3 when something is absent or an installed version differs
  from the lock; `exit_because` says which. `incomplete` lists what was not
  checked (the file limit, the MCP tool's 90-second budget, files that could
  not be read or parsed). `api A.B.C`
  looks up every part: the attributes of a function or variable are not
  listed (`decided: unknown`).
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
  fresh run (a call's keywords by the call's lines).

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
