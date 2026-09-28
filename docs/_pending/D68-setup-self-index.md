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

- **One rule for "Verinoda's own file"** (`verinoda/selffiles.py`, new):
  - every file under the skill folders the installer writes, `.claude/skills/verinoda/` and
    `.agents/skills/verinoda/` (`SKILL_DIRS`, which the installer now takes its skill paths from, so the two
    cannot drift), whatever else a user puts there;
  - a file the project's install manifest (`.verinoda/install-manifest.json`) lists as written whole by
    Verinoda (`kind: "file"`) while it still carries the ownership marker (`<!-- verinoda-managed`). Today
    those are the skills; the rule covers any later installer target outside the skill folders. A listed file
    that lost the marker was taken over by the user and is theirs again. The manifest is read with every
    error caught (a malformed manifest lists nothing) and remembered by its size and time, as is each
    file's marker check, because the file list is taken many times in one analysis.
- **Applied where the corpus is listed, in three places, not per consumer:**
  - `snapshot.list_files`, tracked or not (a team may commit its skill). Everything that reads the file
    list inherits it: the snapshot and its stale-claim diff, the search index's data files, the lexicon's
    candidates, the syntax facts, `treestate` and experiment copies, the UI.
  - The graph build (`project_index/watch._rebuild_code`, Verinoda patch): the rule's paths are added to
    the build's `--exclude` patterns (anchored at the project root). detect() then never reads them, on the
    walk and on the git-listing path alike, and explicit patterns win over git-tracked status. The same
    patterns feed the reconcile's "a live ignore rule matches" test, so the nodes an older graph has from
    those files are evicted on the next build instead of being kept by its fail-closed rule.
  - `freshness.check`: it walks known folders itself and asks git about new files; the root is always a
    known folder, so without the rule a skill would be reported as "changed since the index" on every read,
    and no update could clear it.
  - As the last line of defence, `index._post_process` (which already drops the nodes of missing files)
    drops the nodes of Verinoda's own files from graph.json after every scan/update build and reports them
    as `own_files_dropped`. Needed in practice: an index copied from another folder records that folder as
    the graph's root (`.graphify_root`), and the reconcile then keeps every stored node whose file looks
    outside the scanned folder; on the copy of the 2,623-file project the skill's 16 nodes survived the
    exclude rules until this step was added.
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
  `"full"` when the graph was built, `"none"` when it was left as it was.
- **The AST cache schema is 8** (`project_index/cache._AST_CACHE_SCHEMA`): an unchanged `.mcp.json` would
  otherwise come from the AST cache (keyed by content and schema) with the Verinoda node the old extractor
  made. Adding `mcp_ingest.py` to the extraction stamp would not do: it forces a rebuild, but the rebuild
  would take the old extraction from the cache. The schema is also part of the extraction stamp, so the
  first update after the upgrade rebuilds the graph once, from a cold AST cache.
- Existing projects need nothing: on the next update the own files leave the file list, the snapshot diff
  lists them as removed, the graph is rebuilt (removed files the graph has nodes from), their nodes are
  evicted, and the search index drops the files that left the graph and the list. A later update is a no-op.
- Not changed: `verinoda install` on an already indexed project that creates `.mcp.json` adds a JSON file,
  which counts as code for the graph: one rebuild, once. A skill folder is left out whole, so a file a user
  keeps in `.claude/skills/verinoda/` is not indexed either. User-scope installs write under the home folder
  and are not in any project's corpus.

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
step); the real CLI run twice is a no-op. Existing setup, snapshot, agents, workflow, detect, freshness,
portable-id, docs and packaging tests and the vendored MCP-config tests pass unchanged.

### NN.4 Not done

- The derived-data refresh of an update whose only change is a file no derived index reads much of (here
  `.mcp.json`) still costs about 3.5 s on this project: `lexicon.build` re-associates all units whenever any
  file changed. Skipping that when no lexicon input changed needs a restamp of the lexicon's `tree_hash`
  (analyze rebuilds a lexicon whose tree hash is not the snapshot's) and checks for locale files, the seed
  dictionary and the parameters; it is a change of its own.
- An edited Markdown file rebuilds the whole graph (the side question above).
- In an index copied from another folder, a node the old extractor made from Verinoda's `.mcp.json` entry
  can stay (see Measured); a fresh scan clears it.

## Decision table row

| D68 | Verinoda's own files are not the project's | implemented | Built 2026-09-28 (section NN): the skill folders the installer writes and the files its install manifest lists as its own (with the ownership marker) are left out of the snapshot's file list, the graph (as build excludes that also evict an older graph's nodes, plus a post-build drop) and the freshness check; `.mcp.json` stays in the corpus but Verinoda's own server entry is not extracted and a change confined to it does not rebuild the graph (a digest of the config without that entry is recorded with each build); setup writes the agent files before indexing. On a 2,623-file project a repeated setup no longer rebuilds the graph: 40 s -> 1.3 s for the run after a first setup, 22-23 s -> 5.1-5.5 s for setups alternating two installs (the rest is the derived-data refresh of `.mcp.json`, not changed). |

## UPGRADING note

### D68: Verinoda's own files are not indexed

- Nothing to run. The first `verinoda update` (or `setup`) after upgrading rebuilds the graph once: the
  AST cache schema is 8 (an MCP config's extraction no longer includes Verinoda's own server entry), and the
  skill files leave the index. After it, `.claude/skills/verinoda/`, `.agents/skills/verinoda/` and any file
  the install manifest lists as Verinoda's (with the `verinoda-managed` marker) are in no snapshot, graph,
  search index, lexicon or freshness report. `.mcp.json` stays indexed without Verinoda's `verinoda` entry.
- `verinoda setup` installs the agent files before it indexes. Running it again, from the same or another
  Verinoda install, no longer rebuilds the graph when only those files changed; the setup report's `index`
  has `graph`: `"full"` or `"none"`.
- A scan/update result may carry `own_files_dropped` (graph.json had nodes from Verinoda's own files and they
  were dropped), and `build_stats.json` has `configs` (the MCP-config digests of the last graph build).
- An index copied from another folder may keep the pre-D68 `verinoda` server node of `.mcp.json`; delete
  `.verinoda/index` and run `verinoda scan` to clear it (derived files are disposable).
