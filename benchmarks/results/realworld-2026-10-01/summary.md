# Real-world run 2026-10-01

Verinoda 0.3.2 at 27f72bf, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs. Times in seconds, wall clock, one process at a time.

| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | gold |
|---|---:|---:|---:|---:|---:|---:|---:|
| gin-gonic/gin (Go) | 130 | 12.0 | 7.9 | 1.4 / 2.6 | 0 | 0 | 7/10 |
| junegunn/fzf (Go) | 161 | 16.7 | 8.5 | 1.5 / 3.5 | 0 | 0 | 9/10 |

## gin-gonic/gin v1.12.0

Gold facts:

- hit  `def-default` (q)
- hit  `default-calls-new` (q)
- MISS `default-calls-recovery` (trace): no path (ambiguous)
- hit  `servehttp-calls-handle` (q)
- MISS `request-reaches-tree` (trace): no path (no directed path)
- MISS `get-registers-route` (q): no row
- hit  `recovery-calls-writer` (q)
- hit  `context-json-method` (q)
- hit  `query-radix-tree` (query): rank 1
- hit  `query-recovery` (query): rank 1

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.26 | 48 |
| scan | 0 | 11.97 | 1191 |
| gold:def-default | 0 | 1.14 | 983 |
| gold:default-calls-new | 0 | 1.17 | 1363 |
| gold:default-calls-recovery | 2 | 1.24 | 935 |
| gold:servehttp-calls-handle | 0 | 1.15 | 1568 |
| gold:request-reaches-tree | 2 | 1.25 | 521 |
| gold:get-registers-route | 1 | 1.19 | 803 |
| gold:recovery-calls-writer | 0 | 1.15 | 1475 |
| gold:context-json-method | 0 | 1.16 | 1023 |
| gold:query-radix-tree | 0 | 1.43 | 5279 |
| gold:query-recovery | 0 | 1.30 | 5201 |
| query1 | 0 | 1.29 | 5687 |
| query2 | 0 | 1.40 | 5311 |
| query3 | 0 | 1.36 | 5272 |
| analyze1 | 0 | 2.73 | 11642 |
| analyze2 | 0 | 2.56 | 9087 |
| trace | 2 | 1.24 | 962 |
| q1 | 0 | 1.23 | 1180 |
| q2 | 1 | 1.16 | 718 |
| check | 4 | 0.79 | 1659 |
| map:dependencies | 0 | 1.16 | 8392 |
| map:dataflow | 0 | 1.66 | 3292 |
| map:dead | 0 | 2.27 | 148766 |
| map:hotspots | 0 | 1.42 | 1299 |
| routes | 0 | 1.08 | 252 |
| schema | 0 | 1.24 | 713 |
| doctor | 0 | 2.46 | 9912 |
| mcp:project_query | 0 | 1.41 | 6186 |
| mcp:analyze | 0 | 2.85 | 8737 |
| update | 0 | 7.91 | 1199 |
| review | 3 | 3.57 | 14691 |
| update:revert | 0 | 6.54 | 1199 |

## junegunn/fzf v0.74.4

Gold facts:

- hit  `def-run` (q)
- MISS `main-calls-run` (trace): no path (no directed path)
- hit  `parse-calls-parse` (q)
- hit  `run-calls-postprocess` (q)
- hit  `run-calls-newmatcher` (trace)
- hit  `matcher-loop-scan` (q)
- hit  `def-fuzzymatchv2` (q)
- hit  `pattern-matchitem` (q)
- hit  `query-fuzzy-algo` (query): rank 1
- hit  `query-options` (query): rank 1

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.25 | 47 |
| scan | 0 | 16.73 | 1188 |
| gold:def-run | 0 | 1.17 | 985 |
| gold:main-calls-run | 2 | 1.25 | 313 |
| gold:parse-calls-parse | 0 | 1.17 | 1514 |
| gold:run-calls-postprocess | 0 | 1.16 | 1582 |
| gold:run-calls-newmatcher | 0 | 1.28 | 615 |
| gold:matcher-loop-scan | 0 | 1.18 | 1471 |
| gold:def-fuzzymatchv2 | 0 | 1.21 | 1009 |
| gold:pattern-matchitem | 0 | 1.16 | 1539 |
| gold:query-fuzzy-algo | 0 | 1.42 | 5677 |
| gold:query-options | 0 | 1.41 | 5236 |
| query1 | 0 | 1.50 | 5696 |
| query2 | 0 | 1.54 | 5655 |
| query3 | 0 | 1.33 | 5630 |
| analyze1 | 0 | 4.52 | 10032 |
| analyze2 | 0 | 2.55 | 9575 |
| trace | 2 | 1.22 | 361 |
| q1 | 0 | 1.17 | 1177 |
| q2 | 0 | 1.19 | 1306 |
| check | 4 | 0.81 | 1674 |
| map:dependencies | 0 | 1.12 | 7868 |
| map:dataflow | 0 | 2.31 | 1509 |
| map:dead | 0 | 2.75 | 140635 |
| map:hotspots | 0 | 1.51 | 1299 |
| routes | 0 | 1.12 | 252 |
| schema | 0 | 1.25 | 713 |
| doctor | 0 | 2.47 | 10137 |
| mcp:project_query | 0 | 1.48 | 6272 |
| mcp:analyze | 0 | 4.71 | 8399 |
| update | 0 | 8.53 | 1203 |
| review | 3 | 3.57 | 14150 |
| update:revert | 0 | 7.12 | 1202 |
