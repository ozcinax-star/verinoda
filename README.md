# Verinoda

> **⚠️ Durum / Status: TAMAMLANMADI — work in progress, no guarantees.**
> Bu depo aktif geliştirme altındadır ve henüz bitmemiştir. Özellikler eksik,
> değişken veya hatalı olabilir; hiçbir doğruluk, güvenlik ya da uygunluk
> güvencesi verilmez. Üretimde kullanmayın.
> This repository is unfinished. Features may be missing, change without notice
> or be wrong. No warranty or guarantee of correctness, security or fitness for
> any purpose is given (see also the Apache-2.0 "AS IS" terms in `LICENSE`).
> Not published to PyPI yet. Formerly developed under the working name "RepoAtlas".

### What is in this snapshot (2026-09-24)

| Part | State |
|---|---|
| Graphify port (`verinoda/project_index`, `tests_upstream/`) | done. Upstream suite at port time: 5436 passed / 50 failed, and every failure also fails on unmodified upstream on the same Windows machine; not re-run since (`docs/UPSTREAM.md`) |
| Core: claims, evidence, critique, experiments, research/compare, feedback, memory, installers, MCP server (23 tools) | implemented |
| Round 3: search engine, question plans with Turkish support, reference resolver, trust engine (anchors, entailment, facet-level staleness), runtime observation, precise call resolution | implemented and wired into the CLI, MCP and `analyze`; gaps per decision in `docs/DESIGN.md` ("Implementation status") |
| Product test suite | 1,283 passed, 1 skipped, 2 deselected (slow packaging and installer checks), Windows 11 / Python 3.12, 2026-09-24 |
| Agent integration | Claude Code (`/verinoda`) and Codex (`$verinoda`) verified in real headless sessions with the earlier skill text (`docs/AGENT-VERIFICATION.md`); the round-3 skill text (understand-first and references protocols) has no such record yet |
| Benchmarks (measured, `docs/BENCHMARKS.md`, chars/4 token estimates, gold facts found in the delivered context; no model in the loop) | Graphify's own code (226 files, 37 facts, in-sample): Verinoda text retrieval 36/37 at 1,424 tokens/question, ~0.14 s; Graphify 7/37. Set on Verinoda's own earlier code (33 facts): 25/33 vs Graphify 8/33 and raw reading 9/33; it was held out until the 2026-09-23 ranking change, which was chosen with it in view (22/33 before). Turkish paraphrases of the example app: 32/32. Regressions: `analyze` on the example app 32 -> 31/32, the one-off scan of the large corpus got slower (6.7 s -> 14.7 s cold), and the 2026-09-23 change adds about 0.03 s per retrieval on the large sets |
| Game mods and data files (2026-09-24) | data packs, JSON/yml configs and other data files indexed; resource-id links between code and data; reference trees; Java calls the extractor drops; translation pairs from locale files. New example set `glow_mod` (a small fictional Fabric mod, 50 facts): Verinoda text retrieval 48/50, JSON 44, analyze 43, against raw reading 33 and Graphify 13; at `32a5bd4` it was 34 / 25 / 18. Held out only for its first measurement (46 / 32 / 25). A second set written afterwards and measured once, `forge_mod` (NeoForge, Java and Kotlin, 68 facts): text 63, JSON 45, analyze 38 (at `32a5bd4`: 43 / 28 / 26), raw reading 36, Graphify 12; after two more review rounds that looked at four of its questions (so in-sample), final official run: 66 / 50 / 42. The five earlier sets against the pre-mod baseline `dogfood-2026-09-23`: `graphify_core` analyze 30 -> 29, `graphify_core_tr` JSON 16 -> 15 and text 25 -> 26, `heldout_repoatlas` JSON 20 -> 21, the rest unchanged (`docs/BENCHMARKS.md`, `benchmarks/results/mods-2026-09-24/final/`) |
| Notes and graph view (`verinoda ui`, 2026-09-24) | a note per symbol, file, section and data file with its code and links, the line each link is written on and editor links; notes of your own anchored to the code (up to date / code changed / code gone, `verinoda notes`); local graph per note, global graph at file level, search, file tree; local read-only server, no external assets. Checked in Chrome on the forge_mod example and on Verinoda's own repository (1,115 files in the global graph) |
| Cross-platform | tested on Windows 11 only; the CI workflow (Linux/macOS/Windows) exists but is manual and has not been run |

**Known limitations** (see also `docs/DESIGN.md` for per-decision gaps):

Open findings of the second acceptance audit (2026-09-23 05:00), not yet fixed:

- Under process isolation a test run can still write outside the throw-away copy through pytest `@argsfile` or `--junitxml` indirection; use `--isolation container` for untrusted code.
- Running `scan`/`init`/`observe` with the home directory itself as the project is not refused.
- Questions that are only partly about the code can come back `met` with verified but irrelevant claims.
- The reference resolver can still merge or drop references in some multi-reference sentences. Fixed on 2026-09-24: "PR #123 and issue #456 in psf/requests" bound both numbers to the local project's origin remote (an `owner/repo` after a preposition, or before "reposundaki"/"'teki", is now a repository reference when its sentence is about git objects; fractions, protocols, word pairs and folders such as `1/3`, `HTTP/2`, `read/write`, `services/billing` are not); a bare number keeps the origin remote over a repository named in another sentence, and with two repositories in a sentence each number goes to the one written after it. A name right before a version ("fancylib 2.31") is looked up in the project's ecosystems, then PyPI, npm and crates.io, only with `--network on`; by default it stays unbound, common words and units ("took 2.5 seconds", "macOS 14.2") are never package names, and only a clearly written name ("the X package", a name before `v0.3`) makes the result `partial` with a question.
- Fixed after that audit: user `claim add --kind` with unrelated text no longer verifies; `.verinoda/` and `.git/` files are no longer accepted as evidence; `packaging` is now a declared dependency.

Found by running Verinoda on its own repository (2026-09-23), not yet fixed:

- `update` rebuilds the code graph over the whole corpus when a file of the graph or a new code file changed (unchanged files come from the AST cache): about 33 s on a repository of about 2,100 files, 42 s on Verinoda's own 1,163 files and 94 s on Python's standard library copied as a project (2,305 files, 79,526 nodes), so `verinoda ui --watch` trails an edit by that much; edits to other files (data files, documents outside the graph) only refresh the search index. The upstream incremental pass was faster (about 20 s) but lost the cross-file edges of every file it re-extracted, so after an edit a function's imports and calls into other files were missing until the next full scan (fixed 2026-09-24). `analyze` refreshes first and can spend its default 60 s budget on that; run `verinoda update .` before asking.
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
| `doctor` | Python, package layout, upstream base, graph/snapshot freshness, schema, claim counts, search index, lexicon, precise/SCIP availability, `sys.monitoring`, reference network mode, agent skills, MCP config, optional deps; secrets shown only as set/unset |
| `setup [path] [--agents auto\|all\|none\|claude,codex] [--scope project\|user] [--no-mcp] [--reference PATH[=ALIAS,...]]` | One step per project, safe to re-run: `init`, `scan` on the first run and `update` afterwards, then skills + MCP for the agents found on PATH (default `auto`); prints what is left to do by hand. `--reference` marks a folder of reference code (an original being ported, a vendored or frozen copy) that ranks below the project's own code unless a question names it or an alias (repeatable). Setup also points out folders that look like such a copy (most of their code files sit at the same relative path under a larger folder and hold mostly the same lines) and prints the `--reference` command; it never applies it. Refuses the home directory unless `--allow-home` |
| `init [path]` | Create `.verinoda/` (database, config) |
| `scan <repo> [--force] [--precise] [--scip FILE]` / `update <repo>` | Full / incremental index + snapshot, then the derived search index, lexicon and symbol facts; `update` marks claims whose dependencies changed `stale`. `--precise` resolves the call sites of changed `.py` files; `--scip` adopts a SCIP index you produced `--repo R` works for both as for `query`; `update` without a path takes the nearest project |
| `notes [<repo>] [--changed] [--keep SUBJECT] [--delete SUBJECT]` | Your own notes on the code (written in `verinoda ui`) with their status: `fresh`, `changed` (the code was edited since) or `gone`; `--changed` lists only those and exits 1 when there are any (for CI); `--keep` anchors a note you read again to the code as it is now; `--delete` removes one |
| `ui [<repo>] [--repo DIR] [--port N] [--no-browser] [--graph] [--read-only] [--watch] [--export [FILE] [--open]]` | notes and graph of the project in the browser: a note per symbol, file and data file, local and global graphs, search (see *Notes and graph view*); `--graph` opens on the graph view; `--watch` runs `verinoda update` when files change; `--export` writes the graph and the file notes as one HTML file that opens without a server (`--open` opens it) |
| `map [<repo>] [--repo DIR] [--view …]` | hierarchy, dependencies, dataflow, config, tests, history, impact (`--target`, default: git changes) |
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
change (once the edits stop; one update at a time). `verinoda ui --graph` opens
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
again after `verinoda update`. (The `graph.html` the index step also writes is
the upstream Graphify view, which loads vis-network from a CDN.)

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
