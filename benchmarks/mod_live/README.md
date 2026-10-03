# mod_live: edit-to-fresh benchmark of the `verinoda-live` Claude Code mod

The mod re-indexes once per turn with `verinoda update --fast --repo ROOT` (the graph is rebuilt by a
background `verinoda update`). `run.py` measures that path against a plain `verinoda update` on a twin
copy of the corpus edited the same way (same bytes), step by step.

```
python benchmarks/mod_live/run.py                                   # examples/orders_app, 3 runs
python benchmarks/mod_live/run.py --runs 5 --out benchmarks/results/mod-live-YYYY-MM-DD
python benchmarks/mod_live/run.py --corpus PATH --steps body,batch:20 --work <empty work folder>
```

Steps (each run, in order): `comment` appends a comment line to the first Python file; `body` appends a
new function; `batch:K` appends one to each of K files; `add` writes a new module `bench_mod_<run>.py`;
`del` deletes it again. Tests and the benchmark's own modules are never edited. Twins take turns going
first, so a warm disk cache favours neither.

Per step and mode:

| field | meaning |
|---|---|
| `seconds` | wall clock of the update command: what the mod's status line waits for |
| `graph_pending` | (`fast`) the graph was still behind the tree just after the command (build running or not yet started; the lock alone misses a build that has not taken it yet) |
| `symbol_now` | `query symbol:<new name>` (answered from the graph) returns that symbol as an item right after |
| `text_now` | a plain-text `query <new name>` returns an item whose excerpt defines it, right after |
| `graph_seconds` | (`fast`) command plus the wait until no build runs and no file changed since the latest snapshot |
| `graph_fresh` | `symbol:` finds the new name once the build is done, asked before anything else updates the index |
| `gone_absent` | (`del`) the deleted module's function is no longer returned |
| `leftover_changes` | files a following plain `update` still reported as changed: must be 0 |

`graph_seconds` runs from the start of the command to the first poll that sees the graph caught up,
the freshness queries in between included (the build runs on while they do). Each poll starts a Python
process, so the figure is an upper bound by about one poll (~0.5 s).

A hit is read from `items[].symbol` only: the answer echoes the question, so searching the raw output
for the name would always succeed.

Results go to `results/<name>/result.json` with machine paths replaced by `<TMP>`, `<CORPUS>`,
`<REPO>` and `<HOME>`. Tests: `tests/test_mod_live_bench.py` (the end-to-end run is marked `slow`:
`pytest tests/test_mod_live_bench.py -m slow`).
