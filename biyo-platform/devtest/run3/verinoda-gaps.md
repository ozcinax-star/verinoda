# Verinoda / Symbiosis gaps seen in run 3

Source: benchmark (devtest/run3/bench-*.json, run_benchmark.py over 12 queries) and the phase logs of the symbiosis
agents (symbiosis-r1-data.md, symbiosis-r2-frontend.md, and the run-2 logs). Nothing here is measured on other corpora.

## Benchmark, run 2 -> run 3

| metric | baseline r2 -> r3 | symbiosis r2 -> r3 |
|---|---|---|
| edge recall | 0.38 -> 0.23 | 0.38 -> 0.63 |
| core recall | 0.50 -> 0.33 | 0.55 -> 0.79 |
| typed recall | 0.13 -> 0.08 | 0.12 -> 0.18 |
| direction accuracy | 0.03 -> 0.03 | 0.03 -> 0.03 |
| precision (judged) | 0.97 -> 0.95 | 1.00 -> 1.00 |
| precision (strict) | 0.71 -> 0.75 | 0.86 -> 0.66 |

Reading: symbiosis raised recall and kept judged precision at 1.00, but strict precision fell because 25 of 74 edges
are unjudged. Direction accuracy did not move on either side; the relation step does not yet decide direction from
the corpus, in either variant.

## Gaps in Verinoda that the symbiosis agent hit (from its logs)

1. The CLI `query` returns passage leads (headings and first lines), not relation sentences. Three calls returned
   topics.json lines and the wrong passage; none was used for the relation logic. (about 5.1-5.3k characters each)
2. No claim with a status for ordering cues ("öncesinde", "önce", "anlaşılması için"). The verified/inference
   distinction for relations had to be written by hand in query.py.
3. The MCP tools are bound to the verinoda-mod repo. A question about symbiosis\ got answers from verinoda-mod
   (about 13k candidates, none in symbiosis\). Fixed for the kb project by `verinoda setup` in kb\ (adds .mcp.json),
   but a session in another folder still cannot query it.
4. Query output size: 5k+ characters for 6 hits, which is a large cost when only one sentence is needed.
5. A query call may write under kb\.verinoda\index (observed by the agent, not confirmed).
6. The grade_note field lives in topics.json but the query output has no slot for it; the contract was fixed, so the
   page could not show it. This is a product choice, not a Verinoda gap, but it shows the output has no free field.

## Already on the backlog

- docs/BACKLOG.md has no item for relation or ordering extraction from prose, nor for per-folder MCP binding. Gaps 1-4
  are new candidates for it, not known work.

## Not yet tested (next step if you want it)

- Whether `verinoda analyze` returns relation claims with status and file:line evidence for the ordering cues. This is
  the direct test of gap 1 and 2 and has not been run.
- Whether query writes to .verinoda/index (gap 5) with a before/after file listing.
