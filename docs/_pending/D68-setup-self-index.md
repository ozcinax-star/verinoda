# D68-setup-self-index (pending doc text for the operator to merge)

Section numbers are left as `NN` for the operator (the next free DESIGN.md section).

## DESIGN section

## NN. Verinoda's own files are not the project's: no index of them, no rebuild for them (D68, 2026-09-28)

### NN.1 Why

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

### NN.2 Decisions

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

### NN.3 Measured

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

### NN.4 Not done

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

## Decision table row

| D68 | Verinoda's own files are not the project's | implemented | Built 2026-09-28 (section NN): files carrying the ownership marker in a skill folder of the installer's shape (at any depth, any letter case) or listed by the install manifest are left out of the snapshot's file list (and both sides of the debug ledger's tree diffs), the graph (as build excludes that also evict an older graph's nodes, plus a post-build drop) and the freshness check; `.mcp.json` stays in the corpus but Verinoda's own server entry is not extracted and a change confined to it does not rebuild the graph (a digest of the config without that entry is recorded with each build); setup writes the agent files before indexing. On a 2,623-file project a repeated setup no longer rebuilds the graph: 40 s -> 1.3 s for the run after a first setup, 22-23 s -> 5.1-5.5 s for setups alternating two installs (the rest is the derived-data refresh of `.mcp.json`, not changed). |

## UPGRADING note

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
