# Verinoda

> **⚠️ Durum / Status: TAMAMLANMADI — work in progress, no guarantees.**
> Bu depo aktif geliştirme altındadır ve henüz bitmemiştir. Özellikler eksik,
> değişken veya hatalı olabilir; hiçbir doğruluk, güvenlik ya da uygunluk
> güvencesi verilmez. Üretimde kullanmayın.
> This repository is unfinished. Features may be missing, change without notice
> or be wrong. No warranty or guarantee of correctness, security or fitness for
> any purpose is given (see also the Apache-2.0 "AS IS" terms in `LICENSE`).
> Not published to PyPI yet. Formerly developed under the working name "RepoAtlas".

### What is in this snapshot (2026-09-26)

| Part | State |
|---|---|
| Graphify port (`verinoda/project_index`, `tests_upstream/`) | done. Upstream suite at port time: 5436 passed / 50 failed, and every failure also fails on unmodified upstream on the same Windows machine; not re-run since (`docs/UPSTREAM.md`) |
| Core: claims, evidence, critique, experiments, research/compare, feedback, memory, installers, MCP server (35 tools) | implemented |
| Round 3: search engine, question plans with Turkish support, reference resolver, trust engine (anchors, entailment, facet-level staleness), runtime observation, precise call resolution | implemented and wired into the CLI, MCP and `analyze`; gaps per decision in `docs/DESIGN.md` ("Implementation status") |
| Product test suite | 2,400 passed, 1 skipped, 2 deselected (slow packaging and installer checks; the fourteen browser tests run when Chrome, Edge or Chromium is found), Windows 11 / Python 3.12, 2026-09-26 |
| Agent integration | Claude Code (`/verinoda`) and Codex (`$verinoda`) verified in real headless sessions with the earlier skill text (`docs/AGENT-VERIFICATION.md`); the round-3 skill text (understand-first and references protocols) has no such record yet |
| Benchmarks (measured, `docs/BENCHMARKS.md`, chars/4 token estimates, gold facts found in the delivered context; no model in the loop) | Graphify's own code (226 files, 37 facts, in-sample): Verinoda text retrieval 36/37 at 1,424 tokens/question, ~0.14 s; Graphify 7/37. Set on Verinoda's own earlier code (33 facts): 25/33 vs Graphify 8/33 and raw reading 9/33; it was held out until the 2026-09-23 ranking change, which was chosen with it in view (22/33 before). Turkish paraphrases of the example app: 32/32. Regressions: `analyze` on the example app 32 -> 31/32, the one-off scan of the large corpus got slower (6.7 s -> 14.7 s cold), and the 2026-09-23 change adds about 0.03 s per retrieval on the large sets |
| Game mods and data files (2026-09-24) | data packs, JSON/yml configs and other data files indexed; resource-id links between code and data; reference trees; Java calls the extractor drops; translation pairs from locale files. New example set `glow_mod` (a small fictional Fabric mod, 50 facts): Verinoda text retrieval 48/50, JSON 44, analyze 43, against raw reading 33 and Graphify 13; at `32a5bd4` it was 34 / 25 / 18. Held out only for its first measurement (46 / 32 / 25). A second set written afterwards and measured once, `forge_mod` (NeoForge, Java and Kotlin, 68 facts): text 63, JSON 45, analyze 38 (at `32a5bd4`: 43 / 28 / 26), raw reading 36, Graphify 12; after two more review rounds that looked at four of its questions (so in-sample), final official run: 66 / 50 / 42. The five earlier sets against the pre-mod baseline `dogfood-2026-09-23`: `graphify_core` analyze 30 -> 29, `graphify_core_tr` JSON 16 -> 15 and text 25 -> 26, `heldout_repoatlas` JSON 20 -> 21, the rest unchanged (`docs/BENCHMARKS.md`, `benchmarks/results/mods-2026-09-24/final/`) |
| Notes and graph view (`verinoda ui`, 2026-09-24) | a note per symbol, file, section and data file with its code and links, the line each link is written on and editor links; a 3D graph with a panel that says what is in view, follow, a walk through a file's links, regions, a tour and a watch list; a command bar (`Ctrl+K`, English or Turkish); a preview on hover; notes of your own anchored to the code (up to date / code changed / code gone, `verinoda notes`); impact and path; what changed since the index; the answer to a question; local graph per note, global graph at file level, search, file tree; the open page follows the index (`--watch` also runs `update`); a one-file export; a local server that writes only your notes, no external assets. Checked in Chrome on the forge_mod example and on Verinoda's own repository (1,115 files in the global graph); fourteen browser tests drive it in headless Chrome or Edge (skipped without one); measured on Python's standard library as a project (2,305 files, 79,526 notes: *Notes and graph view*) |
| Answer quality (2026-09-25) | `analyze` carries the passages `query` gives (on the seven public sets 204 -> 267 gold facts, query 265); "met" only on claims about the question (met with none of the gold facts in the claims 2 -> 0); an analysis lists the claims that answer first (the handler's findings, the product's code before tests) and the rest as context; 82 more Turkish stems and multi-part questions; `query`/`queries` and `cluster`/`clustered` one token; `update` of Verinoda's own repository 34 -> 27 s, 21 s when the edit leaves the graph as it was (the command, wall clock, quiet machine; an earlier "35 -> 28 s" came from a timing script that left part of the work out); the first question in a git project indexes it; benchmarks can score a final answer written by any command (`--answer-cmd`; no model run yet). A new in-sample set of Turkish user questions about Verinoda scores 11/30: see *Known limitations* (`docs/BENCHMARKS.md`, Update 2026-09-25) |
| Truth rules (`docs/DESIGN.md` D31, 2026-09-25) | Four ways a false sentence reached a verified or "likely" status are closed. Word overlap with the cited lines never verifies (only a verbatim `contains:` quote, which verifies just the quoted text, or a kind's typed check does); a written claim that says more than its check binds (another callee, a condition, a negation, another file) stays unverified. Written claims bind every role (caller and callee in the text's direction, a config text's subject to the name the read is bound to). `claim add` runs the definitive checks at once and prints their scope (no call in the caller's whole body, a setting bound to another name, a reversed order with `--kind order`, a definition the file does not have). A name written as code that the repository does not have is `not_found` with `did_you_mean` in `analyze`, `plan check` and `trace`, never replaced by a similar name. On a copy of the example app the design pass's 9 false sentences went from 2 verified + 2 "likely" after critique to none (3 contradicted when created); three rounds of adversarial review (about 150 probe sentences) found and closed further ways around the rules. The benchmark sets lose no gold fact. In-sample fixtures; no held-out set of false sentences yet. Since 2026-09-26, outside Python a config or relation claim, and a call hop of a flow, is `strong_inference` at most: a pattern finds the environment read and the syntax tree the call, but nothing binds the claim's subject or caller (a TypeScript "jwtSecret is read from DISCOUNT_THRESHOLD" was `statically_verified`). A static resolver's definitive answer (SCIP) still verifies |
| Name check (`verinoda check`, `verinoda api`, D32, 2026-09-25) | Python only. Since 2026-09-26 a file in another language (named, under a named directory, changed in the diff, or a snippet's `--as`; a notebook and Cython count too) and a Python file that does not parse are listed under `not_checked` with the reason, never parsed as Python and never counted as checked. Exit 3 stays "something absent or a version differs from the lock"; exit 4 means "nothing absent, but something asked for was not checked", so a CI gate or a hook can tell the two apart. A broad `except Exception` no longer counts as a guard for an attribute, keyword argument or dict key inside it (the site stays `absent`; `swallowed_by` names the handler); it still guards an import. Do the modules, imported names, attributes, keyword arguments and constant dict keys code uses exist in the project's own environment (`.venv`/`venv`/`env` or `--env`)? `absent` only when the container's names are all known (closed world), otherwise `unknown` with the reason; nearest real names and where else the name is defined. Nothing from the checked project is imported or run. Measured after three review rounds: in-sample fixture of 144 generated sites (79 invented, 65 real, runtime oracle) 74 of 74 absent verdicts right and 74 of 79 invented names caught; 0 false absents on Verinoda's own package, Graphify's code, standard-library packages and the reviewers' probe sets (about 20,800 sites) and on click/pluggy/h11/starlette (8,525 sites); `check --diff` of 30 changed lines about 1.2 s in a fresh process. 26-44% of sites stay `unknown` by design. Not done: mod config keys and resource ids, JVM jars, JS/TS |
| Decisions stay human (`verinoda decide`, D33, 2026-09-25) | A "should we / which one / how will this scale" question is `human_decision_required`, never `met`: `decide brief` gathers the code's side (forces with evidence, what is absent with what was searched, decisions on record, options) and at most 5 questions only the user can answer, with no recommendation. The user's explicit choice becomes a Markdown decision record (`decided-by: human`) with guards (`only_in`, `no_edge`, `dependency`, `governs`, `revisit_when`); `decide check` finds code that breaks a recorded decision (exit 1 on VIOLATED, exit 3 when something was not checked, usable in CI; `--changed`/`--base` for new ones only). Since 2026-09-26 a guard that checked no file, edge or manifest is `unknown`, never ok, and every ok says what it counted. The records' folder can be committed: `[decisions] dir = "docs/decisions"` in `verinoda.toml`, `[tool.verinoda.decisions] dir` in `pyproject.toml`, or `--decisions-dir`; a folder named there that does not exist, or a fresh clone with no record but ADR-like files, fails with exit 3 instead of passing. Dependency guards read the workspace packages the root declares (globs matched as npm, yarn and pnpm match them) and Gradle version catalogs. Measured on 54 guard mutations over the example projects: 25/25 VIOLATED right, no VIOLATED on benign code (the old regex exclusivity check: 7 right, 8 false alarms, 6 missed on the same orders_app cases). Honest limit: on questions the rules were not tuned on, a choice question is recognised about half the time (the skills tell the agent to run the brief itself for any choice question); a missed one is judged by the intent it got - at most `met_with_inference` when its words may ask for a choice ("best", "smarter"), and it can still end `met` without such words |
| Debug ledger (`verinoda debug`, D34, 2026-09-25) | Every attempt at fixing one symptom is recorded against one repro command: the tree it ran on, the patch against the base, the failure as exception at `file::symbol`, progress. Definitive loop rules stop the agent (tree or file reverted, the same failure back, no progress, a test edited instead of code, edits the failing test never reaches); heuristic ones warn (error moved, masking, a repeated hypothesis). Strategies run in throw-away copies: the repro at the base with the diff's hunks ranked, a bisect that runs both ends first, reruns for flakiness, a traced run. Verinoda never says "fixed": a session closes only on a pass of the repro command on the current tree. In-sample benchmark of 12 scripted sessions: 11/11 definitive findings right, 8/8 loops caught, 0/4 controls stopped; a review found 25 distinct problems (false "passed", false stops, unverified bisect ends), all fixed with tests. Not measured: a real agent with and without the protocol. Found while debugging a flaky test in a real mod (2026-09-26): a Minecraft GameTest run's failures are now read from its summary ("N required tests failed" and the "- ns:test_id: message" lines under it), not from an exception the server logs while it starts, and a test id is mapped to its test method when exactly one test class and method spell it; `debug close --resolved-by` refuses when the baseline passed and every failing run came after an edit (those failures are the edits' own; show the symptom first, for example with `debug rerun` on the base tree, or close with `--abandoned`) |
| Change review (`verinoda review`, D35, 2026-09-25) | For a diff, a staged change or a planned one (`--target FILE::NAME --change body\|signature\|remove`): the changed definitions (comments and whitespace are not changes), who depends on them with the via-chain, and findings by concern: persistence (a write reached, Python value flow), security (risky operations, removed or weakened guards), performance (IO or queries in loops, hot paths), public API (call sites that no longer fit), config and entry points; which tests reach the change (static, observed, `--run-tests`) and which code no test reaches; a `read_first` list packed to a character budget. Measured on labelled fixtures over the example projects: precision 0.92 and recall 62/62 in-sample, precision 0.81 on the held-out set (0.79 on its only clean run, below the 0.8 bar; most false positives are entry points); 67-100% fewer items than the impact view. Unknown stays unknown: "no finding" never means safe |
| Behaviour probe (`verinoda probe`, D36, 2026-09-25) | Python only. For a changed function: inputs from its signature, annotations, call sites and boundary constants, run on the old and the new version in throw-away copies, compared; differences are reported as behaviour changes (not bugs), with the example and an optional pinning test. A side-effect gate refuses functions that write files, open sockets, start processes or keep global state it cannot isolate, and says why. Measured on hand-written fixtures and mutants (in-sample): 22/22 planted regressions and 25/25 mutants found; 5 of 60 behaviour-preserving edits reported, all float rounding marked as such; the gate refused 9 of 9 unsafe functions and none of the safe ones. It says "no difference found in N inputs", never "verified". Since 2026-09-26 an environment variable set while a library module loads (numpy sets `OPENBLAS_MAIN_FREE` when it is imported) is not a side effect, so the functions of a project that uses numpy are no longer all refused; project code that sets one (at import, in a call, or through a library function such as `load_dotenv`) still is |
| Exact names and a fresh index (`docs/DESIGN.md` D37, 2026-09-26) | `trace`, `map --view impact` and MCP `node_inspect` resolve a name the same exact way: a copy of the project that scan detected (or a reference tree) gives way to the original, then definitions in tests, examples, fixtures and vendored folders give way to the product's own, and a helper nested in a function to a module- or class-level one; what is still tied is listed as `ambiguous` (what gave way under `set_aside`) and none is picked, and impact is never computed for a similar name (`...::NoSuchThing` gave 80 affected on the standard library; now it is unresolved, exit 2). `plan check` no longer asks you to choose between a copy and its original, and the receiver-call pass no longer links a copy's calls into the project. One index build runs at a time per project: a second `update` waits and says who is building (no `[WinError 2]`, no `scan --force` hint). `query`, `trace`, `map` and the MCP read tools say "N file(s) changed since the index" with the list; a name only a changed file spells is "not in the index yet", never a similar name. `analyze` does not charge the refresh to its budget and, on a big project where the refresh would be slow, answers from the previous index and names the stale files (the MCP server then updates in the background). The eight benchmark sets find the same facts per question as before |
| Token cost (`docs/BENCHMARKS.md`, "token wins", 2026-09-26) | What a model reads got shorter with no gold fact lost, found or shown, on the eight public sets (86 questions, 319 facts) or on 10 out-of-sample questions (chars/4 tokens). Per question: `analyze` as the skill reads it 3,901 -> 1,873 tokens (-52%: its `--json` before, its text now; 287 facts found, 219 shown), MCP `analyze` 2,832 -> 2,037 with 15 more facts found (287), `query` text 1,313 -> 1,310 with 2 more (285 found, 213 shown); Graphify's two renderers deliver 68-69 found and 8 shown at 1,391-1,480. Per session, before the first question: about 7,330 tokens (the skill ~4,240, the core MCP menu ~2,490 plus ~500 of instructions, `doctor --brief` ~100) against about 20,500 before (the MCP menu of every tool alone ~12,500) and about 11,600-12,000 for Graphify (skill plus MCP menu). Measured on the token-wins branch, when the core profile had eleven tools; `change_review` joined it at the merge and the merged skills are longer, neither measured again. No model in the loop |
| Honest verdicts (D39, 2026-09-26) | `analyze` says `met` only for answers about what was asked. A definition answers only "where is it defined"; "which code uses X" needs code that uses X; "where is X ticked" needs a tick of X itself; claims only from a reference tree, a detected copy or a vendored folder never make it `met`; a callers answer names the call sites the graph did not resolve ("3 call sites unresolved: loop/transport.py:9, ...") and stays `met_with_inference`; a commit line is not a reason; "which X are not listed in Y" is `not_supported`. A verdict only goes down, no claim is dropped. On a public regression set of 17 traps and 22 controls (`verinoda benchmark verdict-audit`, written by the rule author): wrong `met` 9/23 -> 0/23 on dev and 8/16 -> 1/16 on held-out; controls kept 13 -> 12 and 7 -> 7; no benchmark fact lost |
| Cross-platform | tested on Windows 11; CI (Linux, Windows, macOS x Python 3.10/3.12/3.13) runs on every push since 2026-09-25 - its first run found and fixed macOS isolation, macOS path forms in the export and the Python 3.10 call tracer |

**Known limitations** (see also `docs/DESIGN.md` for per-decision gaps):

- A Turkish question about a repository whose code is English but whose UI strings and docs are Turkish (Verinoda itself) finds the Turkish text first: the `verinoda_user_tr` set scores 13/30 (query text and analyze; 11/30 before an English code word in a Turkish question stopped taking the names it merely appears next to, 2026-09-25), against 36/37 on the English `graphify_core`. Each dictionary gloss of a Turkish word still counts as its own search term, which is why adding correct dictionary entries has not helped yet. Two fixes were measured and not kept (`docs/BENCHMARKS.md`, Update 2026-09-25): weighing such words below their translation cost a mod set, where those words are how the data files are found.

- On Windows with Python 3.10 or 3.11, a source file nested thousands of levels deep can crash the extractor: the upstream pipeline raises Python's recursion limit to 10,000, and before Python 3.12 that can exhaust the C stack before a `RecursionError` (found by CI, 2026-09-25; Python 3.12+ and other systems are not affected).

Open findings of the second acceptance audit (2026-09-23 05:00), not yet fixed:

- Under process isolation a test run can still write outside the throw-away copy through pytest `@argsfile` or `--junitxml` indirection; use `--isolation container` for untrusted code.
- Running `scan`/`init`/`observe` with the home directory itself as the project is not refused.
- Questions that are only partly about the code can come back `met` with verified but irrelevant claims.
- The reference resolver can still merge or drop references in some multi-reference sentences. Fixed on 2026-09-24: "PR #123 and issue #456 in psf/requests" bound both numbers to the local project's origin remote (an `owner/repo` after a preposition, or before "reposundaki"/"'teki", is now a repository reference when its sentence is about git objects; fractions, protocols, word pairs and folders such as `1/3`, `HTTP/2`, `read/write`, `services/billing` are not); a bare number keeps the origin remote over a repository named in another sentence, and with two repositories in a sentence each number goes to the one written after it. A name right before a version ("fancylib 2.31") is looked up in the project's ecosystems, then PyPI, npm and crates.io, only with `--network on`; by default it stays unbound, common words and units ("took 2.5 seconds", "macOS 14.2") are never package names, and only a clearly written name ("the X package", a name before `v0.3`) makes the result `partial` with a question.
- Fixed after that audit: user `claim add --kind` with unrelated text no longer verifies; `.verinoda/` and `.git/` files are no longer accepted as evidence; `packaging` is now a declared dependency.

Found by running Verinoda on its own repository (2026-09-23), not yet fixed:

- `update` rebuilds the code graph over the whole corpus when a file of the graph or a new code file changed (unchanged files come from the AST cache): about 33 s on a repository of about 2,100 files, about 27 s on Verinoda's own repository (about 1,200 files), 21 s when the edit leaves the graph as it was (34 s before the per-file caches and the kept graph of 2026-09-25, on the same quiet machine; 44 s before each path was resolved once per build, measured under load) and 94 s on Python's standard library copied as a project (2,305 files, 79,526 nodes; measured before that change), so `verinoda ui --watch` trails an edit by that much; edits to other files (data files, documents outside the graph) only refresh the search index. The upstream incremental pass was faster (about 20 s) but lost the cross-file edges of every file it re-extracted, so after an edit a function's imports and calls into other files were missing until the next full scan (fixed 2026-09-24). `analyze` refreshes first without charging it to its 60 s budget; on a project of 300 or more files where the last graph build took over 15 s (or over 200 files changed, or another build is running) it answers from the previous index and names the changed files instead, and the MCP server starts `verinoda update` in the background. Run `verinoda update .` when you want the answer on the new code. Two builds of one project never run at once: the second waits (the CLI up to 10 minutes) or, where it may not wait, does nothing and says who is building.
- A frozen copy of the code inside the repository (here `benchmarks/corpora/heldout_repoatlas_7371990/`) answered self-queries unless it was marked by hand (`verinoda setup . --reference benchmarks/corpora=heldout,snapshot`). Since 2026-09-24 scan and update find such copies themselves and rank them the same way (see *Reference trees*); a copy they cannot tell apart from the original still needs `--reference`. (Fixed on 2026-09-24: command names now map to their handlers, `scan` komutu -> `cmd_scan`.)

Game mods and data files (2026-09-24):

- The kind of resource an id names is taken from a fixed table of command words and JSON keys; an id in plain Java is "kind not stated" (an inference) unless one file carries it. Bare names count only as an argument of an id constructor or of a helper whose name says what it loads.
- The extra call pass covers Java and Kotlin only. Turkish stems that folding merges (`öl` die / `ol` be) are matched as written only in retrieval; the question plan still reads folded words.

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
It ends with a first question about the project, built from its most connected
function or class, and how to open the graph.

Or skip it and just ask: in a git work tree without an index, the first
`verinoda query`, `analyze`, `trace`, `map` or `ui` indexes the project once and
says so on stderr (never outside a git work tree or in the home folder;
`VERINODA_NO_AUTO_INDEX=1` turns it off):

```bash
cd my-project
verinoda query "where is the discount threshold configured?"   # indexes first, then answers
```

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
is hardlinked or editable (`sandbox_readable`). On such an install the Codex
MCP entry is registered with `--profile full`: the Codex sandbox may not
import the package, MCP is then the way in, and it must serve every tool the
skill uses. Reinstall in copy mode and run install again to get the default
(core) entry.

## Quick start

```bash
cd my-project
verinoda setup                           # once: index + agent skills (or `verinoda scan .` for the index only)
verinoda map . --view dataflow           # entry points -> persistence, with limits stated
verinoda ui                              # notes + graph in the browser (local)
verinoda ui --graph                      # ... opened straight on the graph view
verinoda ui --watch                      # ... and kept up to date while you edit
verinoda ui --export --open              # the graph + file notes as one HTML file, no server
verinoda notes --changed                 # your own notes whose code changed since you wrote them
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
| `doctor [--brief]` | Python, package layout, upstream base, graph/snapshot freshness, schema, claim counts, search index, lexicon, precise/SCIP availability, `sys.monitoring`, reference network mode, agent skills, MCP config, optional deps; secrets shown only as set/unset; `--brief` prints only the graph and snapshot lines and every failed or warning check |
| `setup [path] [--agents auto\|all\|none\|claude,codex] [--scope project\|user] [--no-mcp] [--reference PATH[=ALIAS,...]]` | One step per project, safe to re-run: `init`, `scan` on the first run and `update` afterwards, then skills + MCP for the agents found on PATH (default `auto`); prints what is left to do by hand. `--reference` marks a folder of reference code (an original being ported, a vendored or frozen copy) that ranks below the project's own code unless a question names it or an alias (repeatable). Setup also points out folders that look like such a copy (most of their code files sit at the same relative path under a larger folder and hold mostly the same lines) and prints the `--reference` command; it never applies it. Refuses the home directory unless `--allow-home` |
| `init [path]` | Create `.verinoda/` (database, config) |
| `scan <repo> [--force] [--precise] [--scip FILE]` / `update <repo>` | Full / incremental index + snapshot, then the derived search index, lexicon and symbol facts; `update` marks claims whose dependencies changed `stale`. `--precise` resolves the call sites of changed `.py` files; `--scip` adopts a SCIP index you produced `--repo R` works for both as for `query`; `update` without a path takes the nearest project |
| `notes [<repo>] [--changed] [--keep SUBJECT] [--delete SUBJECT]` | Your own notes on the code (written in `verinoda ui`) with their status: `fresh`, `changed` (the code was edited since) or `gone`; `--changed` lists only those and exits 1 when there are any (for CI); `--keep` anchors a note you read again to the code as it is now; `--delete` removes one |
| `ui [<repo>] [--repo DIR] [--port N] [--no-browser] [--graph] [--read-only] [--watch] [--export [FILE] [--open]]` | notes and graph of the project in the browser: a note per symbol, file and data file, local and global graphs, search (see *Notes and graph view*); `--graph` opens on the graph view; `--watch` runs `verinoda update` when files change; `--export` writes the graph and the file notes as one HTML file that opens without a server (`--open` opens it) |
| `map [<repo>] [--repo DIR] [--view …] [--max-lines N]` | hierarchy, dependencies, dataflow, config, tests, history, impact (`--target`, default: git changes). Text output is a summary per view (counts, the largest folders and packages, the heaviest dependencies, what was left out) instead of a JSON dump; `--json` has everything. The config view also lists `.yml`/`.toml` config files that are not graph nodes. An impact `--target` that does not name one symbol exactly is listed with its candidates (`resolution`) and the command exits 2 |
| `query "<q>" [--max-items N] [--max-chars N]` | Bounded retrieval from the passage index; plain text for a model by default (skeleton first, each item with why it was chosen; nothing printed twice: no question echo, a signature once, windows dedented), `--json` for programs. Says "N file(s) changed since the index" with the list, and "not in the index yet" for a code name only a changed file spells |
| `trace <a> <b> [--mode flow\|any]` | Directed paths, each hop with relation, confidence and call-site location; hints when an endpoint does not resolve; an endpoint that names several symbols is listed (`ambiguous`), none picked; a `resolved:` line names the node each endpoint resolved to; exit 2 when an endpoint does not resolve or is ambiguous, or there is no path |
| `plan draft\|check\|schema\|audit` | Question plans: draft from the message (TR/EN rules), check and ground a plan file, print the schema, re-judge an analysis' sub-questions later |
| `analyze ["<q>"] [--plan FILE] [--run-tests] [--observe] [--refresh auto\|inline\|skip]` | Budgeted loop per sub-question → claims + evidence + critique + unknowns, each sub-question judged against its `done_when`. The default output is the text a model reads: the snapshot, how the question was understood, where its words resolved in the code (`plan links:`), one block per sub-question with its verdict, answer claims and unknowns, the other claims, then the passages `verinoda query` gives; `--json` prints the full record (steps, usage, every field). The index refresh that runs first is not charged to the budget: `auto` skips a slow one on a big project and names the changed files, `inline` always refreshes (waiting for a build already running), `skip` never does |
| `claim show\|list\|add` | Inspect claims, or record one with source evidence (`--kind location\|relation\|config\|order --symbol X` for a mechanical grade; definitive misses are contradicted when the claim is created) |
| `verify <id> [--run]` | Re-check evidence against the current tree (anchored relocation; optionally re-run its test) |
| `challenge <id>` | Critique and counter-hypothesis probes; lowers status/confidence when support is weak |
| `resolve "<text>" [--reference URL[@ref]] [--network off\|cache\|on] [--local-intent]` | Pin every reference in a message to the exact version meant; reports mismatches and questions for the user |
| `research <url-or-repo> [--ref] [--topic] [--resolution ID --reference-id rN]` | Reference repo pinned to an exact commit (or the pin from `resolve`), or a document; mechanism trace |
| `compare <local> <reference> --topic …` | Assumption diff: data structures, errors, concurrency, environment, dependencies |
| `feedback add\|process\|resolve\|show\|list` | User critique handled as a hypothesis → confirmed / qualified / corrected / unresolved (references resolved first) |
| `experiment run --hypothesis … [--ref REF [--overlay PATH]] -- <cmd>` | Isolated targeted experiment on a copy of the working tree, or with `--ref` of one commit (the working tree and `.git` untouched; `--overlay` lays working-tree files over it); logs on disk, summary + evidence + the tree hash of what ran recorded; refused when policy does not allow it |
| `decide brief\|answer\|record\|import\|guard\|accept\|waive\|list\|check` | Decisions stay human (D33). `brief "<question>"`: the code's side of a should/which/scale question and at most 5 questions only the user can answer, never a recommendation (`--argument "NAME: text"` shows the agent's own argument under an option, as weak_inference). `answer <brief-id> --q qN`: the user's answer. `record`: the user's explicit choice as a Markdown decision record with guards, governed symbols and revisit conditions (the decisions folder: `--decisions-dir`, `decisions.dir` in `.verinoda/config.json`, `[decisions] dir` in `verinoda.toml` or `[tool.verinoda.decisions] dir` in `pyproject.toml`, default `.verinoda/decisions/`); `import` a hand-written ADR (guards only proposed); `guard` / `accept` / `waive` / `list`. `check [--changed \| --base REF] [--decisions-dir DIR]`: VIOLATED / POSSIBLE / REVIEW / TRIGGER, ok with scope and limits; exit 1 on VIOLATED, 3 when something could not be checked (status `unknown`), 2 on an error or when a running index build or a failed refresh left `no_edge` guards unchecked (it waits up to 120 s for a build already running) |
| `debug start "<symptom>" [--base REF] [--trace] -- <repro>` / `debug try --hypothesis … [--observed-output FILE --exit-code N -- <the command you ran>] [-- <cmd>]` | Debug ledger (D34): every attempt at fixing one symptom with the tree it ran on, the patch vs the base, the failure at `file::symbol` and progress; definitive loop rules say stop (exit 3), heuristic ones warn. Failures are read from pytest output, Python tracebacks, Minecraft GameTest summaries, JVM, Go, Rust and Node output. A run you made yourself is agent-reported: it never verifies anything and never outweighs Verinoda's own runs of the same tree. A narrowed command's pass is never a pass of the repro; skipping or deselecting the failing tests is caught |
| `debug status\|diff\|close [--resolved-by N [--accept-test-edit]\|--abandoned]` | The ledger; the working tree against the base or an attempt; close (resolved needs a pass of the repro command on the current tree, no Verinoda run of that tree failing, and the user's decision on any test changed since the first attempt; when the baseline passed, a failure seen only after an edit is the edit's own and does not count as the symptom; Verinoda never says "fixed") |
| `debug differential [--trace] [--prepare] [--overlay PATH]\|bisect [--good REF]\|rerun [--times N]\|observe` | Strategies in throw-away copies, each recorded: the repro at the base with the symptom's test held fixed and the diff's hunks ranked; a bisect that runs both ends first; the pass rate; a traced run (are the edits reached, the call chain to the crash) |
| `review [--base REF \| --staged \| --target FILE::NAME --change body\|signature\|remove] [--run-tests] [--observe] [--max-chars N]` | Change review (D35): the changed definitions, their dependents with via-chains, findings by concern (persistence, security, performance, public API, config, entry points), which tests reach the change and which code none reaches, and what to read first; exit 3 when there are findings or unknowns |
| `probe [--changed \| --target FILE::NAME] [--base REF] [--property EXPR] [--emit-test] [--allow-side-effects]` | Behaviour probe (D36, Python): generated inputs on the old and the new version of a changed function in throw-away copies; behaviour differences, new exceptions and non-determinism with examples; refuses functions with side effects it cannot isolate |
| `observe [TEST_ID …] [--for SYMBOL …] [--terms W …] [--mode …]` | Run tests under the call tracer in an isolated copy; reach per test, boundary calls, limits |
| `resolve-call PATH:LINE TARGET [--target PATH:LINE]` | Precise resolution of one call site (needs the `precise` extra; exit 3 = no precise answer) |
| `check [PATH ...] [--diff [REV]] [--stdin --as PATH] [--env auto\|PATH\|none] [--all]` | Python only. Do the names code uses exist? Imports, from-imports, attributes, keyword arguments and constant dict keys, checked in the project's own environment; `--diff` only the changed lines (the default without PATHs; any git diff prefix setting, also from a subdirectory), `--stdin --as` code not written yet. Each site: `exists` / `absent` (with nearest names and where else it is defined) / `unknown` (why) / `not_installed` / `guarded`. Needs the `precise` extra; exit 3 = something absent or a version differs from the lock; exit 4 = nothing absent, but a file asked for was not checked (a file in another language, a notebook, a Python file that does not parse: listed under `not_checked`, never passed) |
| `api NAME [--env ...] [--private]` | Python only. The real members of a module, class or function in the project's environment, with signatures, file:line and the installed version; exit 3 = not found (missing from a module or class whose names are all known, or no such module); a name it cannot decide is `found: null` with exit 0; a name of the project's Java/Kotlin/TS/... code is `found: null` with `decided: unsupported_language`, exit 4 |
| `memory list\|learn\|history` | Versioned learnings, invalidated with their source claim |
| `install/uninstall --agent claude\|codex --scope project\|user` | Skill (+ MCP) for Claude Code (`/verinoda`) and Codex (`$verinoda`) |
| `mcp serve [--profile core\|full]` | MCP server (stdio) over the same core, 35 tools; by default the core profile serves twelve of them (project_query, analyze, node_inspect, relation_trace, map_view, claim_inspect, claim_list, evidence_inspect, index_update, code_check, decision_check, change_review); `--profile full`, or `"mcp": {"profile": "full"}` in `.verinoda/config.json`, serves all. A config whose `mcp` setting cannot be read (not JSON, `mcp` not an object, `profile` not a string or unknown) stops the server with the fix; only a missing setting serves the core profile |
| `index -- <args>` | The Graphify-derived CLI (advanced, unsupported); installer, hook and `~/.graphify` commands are blocked |
| `benchmark run\|sanitize` | Raw search vs Graphify baseline vs Verinoda on question sets with gold facts; `python -m verinoda.benchmark compare A B` prints the totals and, per approach, the facts lost and gained by id (equal totals can hide a swap) |
| `benchmark staleness replay\|mutations`, `benchmark critique-eval`, `benchmark verdict-audit [--split dev\|held_out\|all]` | Staleness harness (history replay, mutation suite), critique precision/recall, and the wrong-`met` verdict audit (`benchmarks/verdict_audit/`) |

Exit codes: 0 done, 1 error, 2 usage error / invalid plan / blocked command /
a `trace` with no path or an endpoint that does not name one symbol / an impact
`--target` that does not name one symbol,
3 "needs more" (clarification, partial resolution, refused experiment,
incomplete observation, no precise answer, an absent name or a lock mismatch in `check`, a name
`api` did not find, a debug attempt that says stop, a debug strategy that did not settle it;
`decide check` exits 1 on VIOLATED, 3 when something could not be checked and 2 on an error);
4 (`check`, `api`): nothing absent, but something asked for was not checked (another language,
a file that does not parse). Every command except `memory`, `mcp serve` and `index` accepts
`--json` (`plan schema` always prints JSON); JSON is compact when stdout is not a terminal.

In CI: commit the decisions folder and name it in `verinoda.toml` (`[decisions]` /
`dir = "docs/decisions"`); run `verinoda decide check --base origin/main` (exit 1 violated,
3 not checked) and `verinoda check --diff origin/main` (exit 3 an absent name, 4 a changed file
it does not read; a clean checkout has nothing changed against HEAD, so `check` then says
`nothing_to_check`).

## Notes and graph view

`verinoda ui` opens the project as linked notes in the browser, in the manner of
Obsidian, built from Verinoda's own index rather than from hand-written notes:

- **A note per symbol, source file, document section and data file**: qualified
  name (`Wisp.spawn()`, `search_index.rank()`), signature, doc text, the code
  (highlighted, with line numbers), and its links in sections: defined in,
  members, calls, called by, extends / implemented by, imports, imported by,
  references, the data files it names by resource id and the lines that name it,
  and the claims recorded about it with their status. A link the tool inferred
  rather than read in the code is marked `?`.
- **Local graph** beside every note (depth 1 to 3; tests, data files and
  external types can be hidden) and a **graph view** of the whole project at
  file level, coloured by folder (or by community), with a filter that
  highlights matching notes; files with no links ring the linked ones, and a
  note's links list the project's own code before tests. Both are force-directed: drag, zoom, hover to see a note's
  neighbours, click to open it.
- **The graph in 3D** (*3D* in the graph view, or `V`): the same files and links
  laid out in three dimensions and drawn with perspective, no WebGL or library;
  the flat graph inflates into depth when you switch. Click a file and the camera
  flies to it; a panel says what it is in words ("extract.py is used by 136 files
  and uses 42"; a document *mentions* files, a data file is *named by* code) and
  numbers its linked files: `1`-`9` fly to one, `N` walks round all of them,
  `Backspace` goes back along your trail, `F` follows the file (the camera circles
  it), `I` lights up what a change to it may affect, `Enter` opens its note. A
  *region* (a folder, or a community when coloured by community) is framed with
  its busiest files and the regions it works with most; `[` and `]` step through
  them, and *Tour* (`T`) visits the product's own regions first, then tests,
  examples and docs, one sentence each. `P` puts the selected file or region on a
  watch list: a chip at the bottom brings you back to it, a small diamond marks a
  watched file, and with the local server the page says when a watched file has
  changed since the index (it looks every 15 seconds; the list stays in your
  browser). Measured on Python's standard library as
  a project: about 60 frames a second in Chrome with 1,745 files and 8,259 links.
- **Command bar** (`Ctrl+K`), in English or Turkish: `focus rank` / `odak rank`
  flies to a note in 3D, `region ui` / `bölge ui` frames a region, `impact store` /
  `etki store` and `path parse to rank` / `yol parse ile rank` light up the files
  concerned, `tour`, `changed`, `open …`, or a question, which is answered as
  below; with an empty bar it lists what it can do. `?` shows every shortcut;
  `Esc` undoes one step at a time (the tour, what is lit, the view).
- **Search** by name (exact, prefix, part of the name, path) or with a question,
  which runs the same ranking as `verinoda query`; a file tree; back and forward;
  Turkish and English; light and dark. A question (three words or more, a
  question word, or a `?`) also offers **Answer the question**: the passages
  `verinoda query` answers with, each with its lines (highlighted, opening the
  editor), why it was chosen and its note, then the other places found. The
  search index is never written for it (not in an exported file: it has no code).
- **The line a link is written on** under each call, import, reference and
  resource-id link (not for a file edited since the last index: its line numbers
  would point elsewhere), and **open in your editor**: every `file:line` and a
  button on each note open VS Code, Cursor or VSCodium at that line (chosen at the
  top of the page).
- **Preview on hover**: resting the mouse on a link to a note shows its kind,
  file and line, signature, first doc lines, your note on it, its link counts and
  the first lines of its code, without leaving the page.
- **What changed**: *Changed* in the graph view rings the files edited, added
  or deleted since the index (what `verinoda update` would take in) and, in
  another colour, the files that use them; a note whose file changed since the
  index says so, since its links and lines may be off.
- **Impact and path**: *Impact* on a note lists what may be affected when it
  changes: what calls, imports, extends or names it, then what uses those, up to
  three links back (the project's own code first, tests on or off), and turns the
  local graph into that set; *Path…* finds the shortest chain of calls, imports
  and references from the note to another one, or the other way round. In an
  exported file both work at file level.
- **Notes of your own** on any symbol, file, section or data unit: plain Markdown
  (`**bold**`, `` `code` ``, lists, `[[Name]]` links another note), kept as `.md`
  files in `.verinoda/notes/` (`notes.dir` in `.verinoda/config.json` puts them
  in a folder you commit). Each note is anchored to the code it was written about
  (a symbol or section by its fingerprint, found again wherever it moved; a whole
  file or a data unit by a hash of its lines) and shows its status: *up to date*,
  *code changed* (read it again, then *Read it: still right*, which anchors it to
  the code as it is now) or *code gone* (the symbol was renamed or deleted: edit
  it onto something else or delete it). The start page lists your notes, the
  changed ones first; `verinoda notes --changed` does the same on the command line
  and exits 1 when any note needs reading again, for CI.

It is local: a standard-library server on `127.0.0.1` (a free port unless
`--port` is given) that answers only requests addressed to that host and port;
the page loads nothing from outside (no CDN, fonts or telemetry;
`Content-Security-Policy: default-src 'none'`, scripts and styles only from the
server). The one thing it writes is your notes: `POST /api/usernote` needs the
random token of that server run, which only the page it serves carries, JSON, and
this origin, so another site cannot write through it; `--read-only` turns writing
off. It follows the index: a few seconds after `verinoda update` (or any
rebuild) the open page redraws the note or graph it shows, keeping its scroll
position, and says so (an unseen tab looks when it is shown again).
`verinoda ui --watch` also runs `verinoda update` itself when the project's files
change (once the edits stop; one update at a time; while another process
builds the index, the watcher skips that round, the page says why, and it
tries again at its next look). `verinoda ui --graph` opens
straight on the graph view.

**One file, no server.** `verinoda ui --export [FILE]` writes the graph view and
a note per source file, document and data file into one HTML file (default
`.verinoda/index/verinoda-graph.html`; about 4.4 MB for Verinoda's own 1,150
files) that opens with a double click, or with `--open` right away; the command
also prints its `file:///` address. Checked in Chrome and Edge from `file://`.
It is the same page with its data inside:
the graph with its filters and colours, the file tree, each file's links, outline
and claims, a file-level local graph and a name search (a symbol opens the note
of its file). It holds no code (`verinoda ui` shows it) and no path of the
machine it was made on (the project root and the home folder are taken out of
every name); its Content-Security-Policy allows only its own script and style
(by hash) and no connections, so it fetches nothing. It is a snapshot: export
again after `verinoda update`. (The index step no longer writes the upstream
Graphify `graph.html`, which loaded vis-network from a CDN when opened, and
removes an old one; `GRAPHIFY_VIZ_NODE_LIMIT` set to a positive number keeps it.)

Limits: the global graph shows at most 2,500 files (the best connected ones,
and it says how many it left out); code is read, not edited; the exported file
has file notes only (no symbol notes, no code, no question search) and shows
your notes read-only.

Measured on Python's standard library copied as a project (2,305 files, 79,526
notes, 140,342 links; Windows 11, headless Chrome): the server starts in 1.6 s,
the start page shows in 1.5 s, a search in 0.8 s, a note in 0.5 s; the graph
view (1,745 files, 8,249 links) draws in 0.4 s and runs at about 60 frames a
second while it settles; impact and path on the most connected notes take
under 20 ms. `--export` writes 9.3 MB in about 6 s. With `--watch` each look at
the tree takes 0.2 s, and an update takes as long as `verinoda update` (above,
*Known issues*).

## Game mods, data packs and other data files

Code often names its data only through strings: a Minecraft mod runs the
data-pack function `mymod:wisp_death`, loads `config/mymod.yml`, registers
the item whose model is `assets/mymod/models/item/x.json`. Verinoda indexes
those files too and follows the strings between them.

- **Data files are searchable.** Text files the code graph has no node for
  (`.mcfunction`, JSON, YAML/TOML/INI/properties configs, SQL, shaders, CSV,
  Gradle scripts, skipped sources) become `data` units; configs are split by
  top-level section. Files it leaves out are listed with the reason
  (`binary`, `may hold secrets` - the graph's own secret rule -,
  `.graphifyignore`, dependency or build-output folder, generated output such
  as `results/` or `logs/`, large generated JSON, minified); `doctor` counts
  them and a query names a left-out file whose name matches the question.
- **Resource ids link code and data** in repositories that are packs or mods
  (a `pack.mcmeta` or a Fabric/Quilt/Forge/NeoForge manifest): `ns:path`
  ids, `#ns:tags`, worldgen ids, `function ns:x`, translation keys
  `"item.ns.x"`, `Identifier.of("ns", "x")`, full asset paths, and bare names
  passed to an id constructor or a helper whose name says what it loads
  (`runFunction(server, "wisp_death")`). The context picks the registry
  (`advancement revoke ... only ns:x` names an advancement, `"parent"` a
  model). Query output shows `names:` / `named by:` lines; a link whose
  namespace is assumed or whose kind the line does not state is marked
  inferred, and an `analyze` claim built from it is `strong_inference`.
- **Identical copies** of a data file (a data pack shipped twice) rank once,
  as the copy in the source set; the others are listed as `same content:`.
- **Java and Kotlin calls** the extractor drops (a class name that exists twice
  in the repository, calls through typed variables, Kotlin calling Java) are
  added when the file's imports or package bind the class, and graded like any
  call site.
- **Callbacks** (`docs/DESIGN.md` D38): a method reference passed
  on (`END_SERVER_TICK.register(RepairScheduler::tick)`,
  `createTickerHelper(..., Block::serverTick)`) is a `registers` edge, never a
  call and never weighed by the ranking. `trace` follows it when no call path
  exists and labels the hop `callback`; impact, the UI and `review` list the
  method that registers a changed one; a claim "A calls B" that only a method
  reference supports stays `weak_inference`. `map --view dataflow` starts at
  the mod's entry points (fabric.mod.json, Fabric initializers, `@Mod`,
  `@SubscribeEvent`, mixin handlers, registered callbacks) and knows JVM
  file writes, `NbtIo` and dirty flags; all of these are text heuristics,
  each with its reason.
- **Reference trees**: `verinoda setup --reference original-plugin/=original,plugin`
  keeps an original implementation searchable but ranks it at 0.6x unless
  the question says "original", "plugin" or the folder name. A folder that holds
  a copy of the project's own code (a benchmark corpus with an older version, a
  vendored snapshot) is found at every scan and update and ranked the same way:
  most of its files have a twin elsewhere defining the same names, nothing outside
  it uses it, and the twins are in code the project does use (`copies.json`;
  `index.not_copies` or `index.detect_copies: false` in `.verinoda/config.json`
  undo it). A port next to its original with nothing else using either is left
  to `--reference`.
- **Turkish names from the repository**: parallel locale files
  (`lang/en_us.json` + `lang/tr_tr.json`, `locales/en.json` + `locales/tr.json`)
  teach the lexicon that "Fener Asası" is `lantern_staff`.

`examples/glow_mod/` is a small fictional Fabric mod with a data pack, a
config file, a reference tree and a copied data pack; its question set
(`glow_mod`, 14 questions, 7 Turkish) was written without running Verinoda
on it. Results: `docs/BENCHMARKS.md`.

## Coding agents

```bash
verinoda install --agent claude --scope project   # .claude/skills/verinoda/SKILL.md + .mcp.json entry
verinoda install --agent codex  --scope project   # .agents/skills/verinoda/SKILL.md (+ MCP config)
verinoda uninstall --agent claude --scope project # removes only what install recorded
```

- **Claude Code**: `/verinoda how does checkout reach the database?`
- **Codex**: mention `$verinoda` in the prompt. (Codex has no `/verinoda` command.)

The skills describe the working method; all logic lives in the CLI/MCP core.
They read the CLI's plain text and add `--json` only for a field the text
leaves out (JSON cost 2-5x the tokens for the same content).
Two protocols come first:

1. **References the user gives.** When the message has links, repository or
   package names, versions, commits, PR/issue numbers, papers or docs, the
   agent runs `verinoda resolve "<message>"` (MCP `reference_resolve`)
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

3. **Check the names code uses (Python).** After every edit, and before
   proposing code, the agent runs `verinoda check --diff` (MCP
   `code_check`; code not written yet: `--stdin --as <path>`). It never keeps
   an `absent` site: it fixes it from `nearest` / `elsewhere` or from
   `verinoda api <module.or.Class>` (MCP `api_members`). `unknown` is
   unverified, not fine. The report names the environment it checked.
   It is Python only: a Java, Kotlin or TypeScript file, or one that does not
   parse, comes back under `not_checked` (exit 4), and the agent says so
   instead of reporting a pass.
4. **Confirm your own sentences.** A sentence the agent writes about the code
   is recorded with a typed kind (`claim add --kind relation|config|order|location
   --symbol X`) or a verbatim quote; plain prose is at most `weak_inference`,
   and a `not_found` name is reported, not replaced.

5. **Decisions are the user's.** For a should-we / which-one question the
   agent runs `verinoda decide brief`, asks the `questions_for_human`, records
   the user's explicit choice (`decide record`) and never picks for them;
   before finishing a code change it runs `verinoda decide check --changed`.
6. **Keep a debug ledger.** `debug start` before the first edit of a bug fix,
   `debug try --hypothesis` after every edit; on `stop` it stops editing and
   follows the first strategy, and it asks before changing a test's expectation.

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
- **Word overlap never verifies.** Every word of "apply_discount returns the
  subtotal above the threshold" is in the lines that return `subtotal * 0.9`
  there. Term coverage makes evidence relevant (`partial`), never a
  verification; only a verbatim quote (`path:12 contains: <text>`, which
  verifies the quoted text and nothing around it) or a kind's typed check
  (call site, definition span, environment read, call order) verifies. A
  written claim that states more than its check binds (another callee, a
  condition or bound, a negation, the arguments of a call, another file than
  the cited one) stays unverified.
- **Every role is bound.** A written relation must name the caller and the
  callee in the right direction ("OrderRepository.save calls place_order" is
  checked against `save`'s body); a written config claim ("the discount
  threshold is read from ORDERS_MAX_ITEMS") must be about the name the read is
  bound to. `claim add` answers a definitive miss at once, with its scope:
  "no direct call to save in create_order_handler (orders/api.py:16-21); calls
  through other names are not followed".
- **A name written as code is never replaced by a similar one.** `analyze`,
  `plan check` and `trace` report `not_found` with `did_you_mean` ("no symbol
  named `place_orders` in this repository; nearest: place_order
  (orders/service.py:19)"); the sub-question is `unmet`. A name spelled only
  in a file changed since the index is "not in the index yet"; one the index
  spells but has no symbol for (a constant, an attribute) is `not_a_symbol`,
  with where it occurs. `trace`, `map --view impact` and `node_inspect` share
  one resolver: a detected copy of the project gives way to the original,
  test, example, fixture and vendored code to the product's own, and a name
  that several symbols still carry is listed, not picked (pass
  `path/file.py::Name`).
- **A stale index is never silent.** Reading commands list the files changed
  since the index; `analyze` says when it answered from the previous index,
  and a changed file that spells the question's subject caps that
  sub-question at `met_with_inference`.
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

## Name check (Python)

`verinoda check` answers one question for code an agent (or you) just wrote: do
the modules, functions, methods, keyword arguments and dict keys it uses exist,
in this project's environment?

    $ verinoda check orders/ai/export.py
    orders/ai/export.py:7:28  ABSENT  import orders.service.place_orders
        not found in module orders.service in this project (orders/service.py)
        nearest: place_order (orders/service.py:19)
    orders/ai/export.py:15:43  ABSENT  kwarg compute_total(currency=)
        keyword currency= not found in the signature compute_total(items: list[dict]) (orders/pricing.py:6)
    orders/ai/export.py:19:10  unknown  attribute repo.save_order
        `repo` is a parameter: its runtime type is not known

- **Which environment:** `--env PATH`, else the project's `.venv`, `venv` or
  `env` when its base interpreter is a known Python installation outside the
  project (otherwise the note names the program `--env .venv` would start),
  else Verinoda's own interpreter for the standard library only: third-party
  names are then `not_installed`, never `absent`. The MCP tools never start a
  program from the project.
- **When it says absent:** only when the container's names are all known (a
  module without `__getattr__` or dynamic writes, a class without descriptors
  or code that sets attributes from outside, an instance made right there, one
  known signature without `**kwargs`, the dict literals a function returns),
  and jedi also found nothing. The wording is "not found in <container> as
  installed in <env> (<file>)", never "does not exist".
- **Unknown is not fine:** parameters, annotations, inferred return values,
  `**kwargs`, module `__getattr__`, names assigned elsewhere, `sys.path`
  changes in `conftest.py`, and standard-library names of another platform or
  Python version (`collections.Mapping`) stay `unknown` with the reason.
- **What it does not read is never a pass:** a file in another language, a
  notebook, a Cython file or a Python file that does not parse is listed under
  `not_checked` with the reason; with nothing absent the exit is 4 (3 means an
  absent name or a version that differs from the lock).
- `verinoda api packaging.specifiers.SpecifierSet` lists the real members
  before a call is written. Existence and signature shape only: a real name
  used wrongly is not detected.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — modules, state on disk, invariants
- [docs/DESIGN.md](docs/DESIGN.md) — design decisions D1-D40 and their implementation status
- [docs/BENCHMARKS.md](docs/BENCHMARKS.md) — measured comparison (no unmeasured savings claims)
- [docs/UPSTREAM.md](docs/UPSTREAM.md) — Graphify base commit, feature inventory, port method, runtime patch
- [docs/UPGRADING.md](docs/UPGRADING.md) — versioning, schema migrations, calibration changes, derived files
- [docs/AGENT-VERIFICATION.md](docs/AGENT-VERIFICATION.md) — what was verified with the real agents
- [docs/GENEL-BAKIS.md](docs/GENEL-BAKIS.md) — Türkçe genel bakış (ürün sahibi için)
- [docs/NAMING.md](docs/NAMING.md) — name availability

## License

Apache-2.0 (see `LICENSE`); portions originally under MIT (`LICENSE-MIT`).
`NOTICE` records the Graphify origin and the modifications.
