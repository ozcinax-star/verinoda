# Backlog 1.1, stage A: a faster `update` with the same outputs (in progress, 2026-09-30)

Work paused here. This file says what was measured, what exists on which branch, what is NOT verified yet, and
what comes next. Nothing below is merged into `competitor-backlog` or `main` except this folder, the backlog and
the `.gitignore` line for agent worktrees.

## Goal

Backlog item 1.1 ("update proportional to the change") in two stages:

- **Stage A:** make `verinoda update` faster while everything it writes stays byte-identical to the unchanged code's
  output (graph.json, sidecars, labels, reports, search.db, atlas.db, lexicon ...).
- **Stage B:** make the cross-file passes incremental (docs/DESIGN.md section 17.1: about 20 passes, communities
  may drift). Stage A alone will not meet 1.1's "done when" (seconds per edit); the backlog row stays.

## Measured (Windows 11, this repository, 2,588 tracked files)

| What | Result |
|---|---|
| `update` after adding one function to `verinoda/buildinfo.py`, wall | 78 s (79 s after reverting it) |
| the same under cProfile | 181.8 s: graph rebuild 151 s (extract 74 s, of which AST cache loads 25 s; `build_from_json` 16 s; language resolvers 10 s; clustering 8.5 s), derived indexes 15 s |
| full `scan` | 124 s |
| copy of the repository outside OneDrive, `update` after the same edit | 82 s: OneDrive is not the cause |
| first open of a freshly written AST cache entry | about 12 ms per file, per file not per byte; later opens under 1 ms. Inferred (not proven): antivirus on first open. Costs 13-16 s on the first `update` after a scan |
| `detect.ignored_predicate` | built twice per `update` (0.67 s for the gitignore one), called 0 times |

Details: `stall_experiment.md`. The pipeline map and the ranked list of 23 graph-preserving changes (each with its
locations, why the output stays identical, and how to verify it): `analysis.json` (ranked) and `subsystem_maps.json`
(five subsystem maps and a completeness critic). Expected saving of all ranked items: about 55-60 s under the
profile, estimated 20-25 s wall; not measured yet.

## Branches (local and pushed to origin; none merged)

All start at `cc71f33` on `competitor-backlog`.

| Branch | Commit | Ranked items done | Skipped |
|---|---|---|---|
| `stagea-watch` | c91f284 | 1 (count check before the canonical compares), 4, 21(c), 23 (lazy `ignored_predicate`) | - |
| `stagea-cluster` | f3de6f0 | 6, 8 | - |
| `stagea-build` | 806b448 | 5, 7 | - |
| `stagea-extract` | b5e7523 | 9, 10, 13, 14, 16 | - |
| `stagea-cache` | ab89e85 | 12, 17, 18 (in-memory entries for long-lived processes) | - |
| `stagea-owned` | d421fc1 | 2, 3, 11, 15, 21(b) | 19(b) (a Graph can carry memos `load` would not have), 22 (would change the lexicon.json format) |
| `stagea-harness-r3` | 26abe86 | `tools/update_equality.py`, the equality harness (contains the r1/r2 rounds) | - |

Each group branch passed its own targeted tests as run by the implementing agent (`implementation.json`, key
`groups`); those runs were not repeated independently. **No group has been verified against the baseline yet.**

## The equality harness is not trustworthy yet

`tools/update_equality.py` runs the baseline and a candidate checkout on copies of a corpus (fixtures, orders_app,
optionally the repository itself), applies the same edits, runs `update` on each and compares what they wrote. It
catches the planted mutants of rounds 1-3, and baseline vs baseline exits 0. Three fix and review rounds did not make
it trustworthy: the reviews found 6, then 8, then 12 defects, each round a wider class. Open after round 3
(`implementation.json`, key `harness_reviews`, last entry):

- n9, the important one: every `update` runs in a fresh CLI process, so a memo that outlives one build (rank 18 invites
  exactly that) is never exercised. Needs a mode that runs several updates in one process.
- n1: the AST cache folder is not compared, and the repository never moves (a cache entry with absolute ids passes).
- m4: the count-only topology check passes on fixtures (caught on orders only).
- writes outside the watched folders, `.git/index`, lock file contents, SQLite free pages, NTFS alternate streams, empty
  folders, file attributes; and a false positive when a candidate changes an extractor file without changing behaviour.

The two verification runs that started were cut short and returned no verdict (not a difference).

## Next steps

1. Decide the harness scope instead of chasing every class: what `update` may write (`.verinoda/`, nothing else),
   plus the several-updates-in-one-process mode (n9), the AST cache comparison (n1) and m4. Then one more adversarial
   round.
2. Verify each group branch with it (fixtures and orders, seeds 1 and 2, warm and cold cache); repair or drop items that
   differ.
3. Merge the verified groups into one integration branch; run the full product suite and `tests_upstream`; time
   `update` on a copy outside OneDrive against the 78-82 s baseline, sequentially on a quiet machine.
4. Docs at integration: a D-number in docs/DESIGN.md, docs/UPGRADING.md, docs/UPSTREAM.md (the groups already list their
   local changes there, one section each, to be merged).
5. Then the packed AST cache (one file instead of about 1,240; the measured 13-16 s first-update cost), which the
   analysis had rejected before the stall was measured.

How the work was run: agent workflows (understand, prep, implement) with a frozen baseline checkout of `cc71f33`; the
group branches were made in separate git worktrees. The venv has Verinoda installed editable from the main checkout, so
any other checkout must be run with `PYTHONPATH=<checkout>` from its root.
