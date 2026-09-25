# `verinoda update` time, second round, 2026-09-25

Two identical copies of Verinoda's own repository (about 1,200 files, indexed) were edited the same
way (same bytes, same mtime) and updated with the command itself, `python -m verinoda update --json`
(wall clock of the subprocess), once with the code before the change ("before", commit d3165b8) and
once with the code after it ("after"). Both copies ran at the same path, one after the other; the
order alternated from step to step. Other agents were running on the 6-core machine, so absolute
times move by several seconds; compare within a pair.

After every step the two copies' outputs were compared: graph.json byte for byte, GRAPH_REPORT.md,
`.graphify_labels.json` and its `.sig`, `.graphify_root`, the dated backup folder, the receiver
sidecar (the graph's `mtime_ns` aside), search.db (every table dumped and sorted; the graph's
`mtime_ns` and `built_at` aside), lexicon.json (`built_at` aside), manifest.json (the `seen` times
aside), python_facts.json, copies.json and the latest snapshot's counts, tree hash and commit.
`outputs` is `identical` when all of them were; `differences` lists what was not. At the end of each
run `verinoda query` was run on both copies for three questions and its output compared.

Steps: `comment:N` rewrites a comment line of `verinoda/textnorm.py` in place (no line moves);
`body:N` inserts a comment line into a function body (every later line moves); `add:N` / `del:N`
add and delete a small module; `delx:PATH` deletes a module other files import; `json:N` changes a
value in a data-shaped JSON file (no graph rebuild); `jsoncode:N` does that and `comment:N` together;
`md:N` rewrites a README.md line in place; `mdshift:N` inserts a line near its top; `scan`,
`scanforce` run `verinoda scan` (`--force`); `fresh` removes `.verinoda/index` and scans.

- `1-cache-paths.json`: after = cached source paths made absolute once per path (commit 1f6668b)
- `2-one-rewrite.json`: after = that, and graph.json read once and written once after the build (c186b01)
- `3-graph-kept.json`: after = those, and the upstream "topology unchanged" path let run (1b3a130)
- `4-all-four.json`: after = those, and skipped data JSON remembered, small batches in-process (b3dfff3)

Each file: a list of `{step, before_s, after_s, outputs, differences}` and, last, `{query, outputs}`.
See docs/BENCHMARKS.md (Update 2026-09-25, *Update time, second round*).
