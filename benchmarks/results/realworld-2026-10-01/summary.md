# Real-world run 2026-10-01

Verinoda 0.3.2 at b4b6b5a, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs.

Times in seconds, wall clock, one process at a time. Gold v1: the frozen facts; v2: the corrected checks added after review.

| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | clean after | gold v1 | gold v2 |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| gin-gonic/gin (Go) | 130 | 11.9 | 7.0 | 1.4 / 2.6 | 0 | 0 | yes | 7/10 | 1/2 |
| junegunn/fzf (Go) | 161 | 14.5 | 7.5 | 1.5 / 3.4 | 0 | 0 | yes | 8/10 | 3/3 |

## gin-gonic/gin v1.12.0

Verinoda 0.3.2 at b4b6b5a, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-01T23:48:55.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-default` (q)
- hit  `default-calls-new` (q)
- MISS `default-calls-recovery` (trace): source not resolved (ambiguous)
- hit  `servehttp-calls-handle` (q)
- MISS `request-reaches-tree` (trace): no path (no directed path)
- MISS `get-registers-route` (q): no row
- hit  `recovery-calls-writer` (q)
- hit  `context-json-method` (q)
- hit  `query-radix-tree` (query): rank 1
- hit  `query-recovery` (query): rank 1
- hit  `default-calls-recovery@v2` v2 (trace)
- MISS `request-reaches-tree@v2` v2 (trace): no path (no directed path)

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.26 | 48 |
| scan | 0 | 11.93 | 1188 |
| gold:def-default | 0 | 1.18 | 983 |
| gold:default-calls-new | 0 | 1.15 | 1363 |
| gold:default-calls-recovery | 2 | 1.28 | 935 |
| gold:servehttp-calls-handle | 0 | 1.24 | 1569 |
| gold:request-reaches-tree | 2 | 1.25 | 521 |
| gold:get-registers-route | 1 | 1.15 | 803 |
| gold:recovery-calls-writer | 0 | 1.24 | 1475 |
| gold:context-json-method | 0 | 1.16 | 1023 |
| gold:query-radix-tree | 0 | 1.38 | 5279 |
| gold:query-recovery | 0 | 1.32 | 5201 |
| gold:default-calls-recovery@v2 | 0 | 1.21 | 513 |
| gold:request-reaches-tree@v2 | 2 | 1.25 | 521 |
| query1 | 0 | 1.48 | 5687 |
| query2 | 0 | 1.39 | 5311 |
| query3 | 0 | 1.34 | 5272 |
| analyze1 | 0 | 2.65 | 11641 |
| analyze2 | 0 | 2.53 | 9087 |
| trace | 0 found | 1.22 | 719 |
| q1 | 0 | 1.16 | 1179 |
| q2 | 1 | 1.20 | 718 |
| map:dependencies | 0 | 1.11 | 8392 |
| map:dataflow | 0 | 1.64 | 3292 |
| map:dead | 0 | 2.29 | 148766 |
| map:hotspots | 0 | 1.35 | 1299 |
| routes | 0 | 1.05 | 252 |
| schema | 0 | 1.21 | 713 |
| doctor | 0 | 2.40 | 9921 |
| mcp:project_query | 0 | 1.25 | 6186 |
| mcp:analyze | 0 | 2.67 | 8737 |
| update | 0 | 7.02 | 1199 |
| review | 3 | 2.99 | 14691 |
| update:revert | 0 | 5.96 | 1199 |

## junegunn/fzf v0.74.4

Verinoda 0.3.2 at b4b6b5a, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-01T23:50:07.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-run` (q)
- MISS `main-calls-run` (trace): no path (no directed path)
- hit  `parse-calls-parse` (q)
- hit  `run-calls-postprocess` (q)
- MISS `run-calls-newmatcher` (trace): no path edge at src/matcher.go
- hit  `matcher-loop-scan` (q)
- hit  `def-fuzzymatchv2` (q)
- hit  `pattern-matchitem` (q)
- hit  `query-fuzzy-algo` (query): rank 1
- hit  `query-options` (query): rank 1
- hit  `main-calls-run@v2` v2 (trace)
- hit  `run-calls-newmatcher@v2` v2 (trace)
- hit  `pattern-matchitem@v2` v2 (q)

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.22 | 47 |
| scan | 0 | 14.51 | 1188 |
| gold:def-run | 0 | 1.19 | 984 |
| gold:main-calls-run | 2 | 1.22 | 313 |
| gold:parse-calls-parse | 0 | 1.17 | 1514 |
| gold:run-calls-postprocess | 0 | 1.17 | 1582 |
| gold:run-calls-newmatcher | 0 | 1.26 | 615 |
| gold:matcher-loop-scan | 0 | 1.17 | 1470 |
| gold:def-fuzzymatchv2 | 0 | 1.17 | 1009 |
| gold:pattern-matchitem | 0 | 1.16 | 1539 |
| gold:query-fuzzy-algo | 0 | 1.41 | 5677 |
| gold:query-options | 0 | 1.40 | 5236 |
| gold:main-calls-run@v2 | 0 | 1.23 | 484 |
| gold:run-calls-newmatcher@v2 | 0 | 1.24 | 542 |
| gold:pattern-matchitem@v2 | 0 | 1.15 | 1539 |
| query1 | 0 | 1.50 | 5696 |
| query2 | 0 | 1.51 | 5655 |
| query3 | 0 | 1.31 | 5630 |
| analyze1 | 0 | 4.43 | 10034 |
| analyze2 | 0 | 2.42 | 9576 |
| trace | 0 found | 1.22 | 683 |
| q1 | 0 | 1.15 | 1176 |
| q2 | 0 | 1.20 | 1306 |
| map:dependencies | 0 | 1.18 | 7868 |
| map:dataflow | 0 | 2.31 | 1509 |
| map:dead | 0 | 2.70 | 140635 |
| map:hotspots | 0 | 1.33 | 1299 |
| routes | 0 | 1.05 | 252 |
| schema | 0 | 1.21 | 713 |
| doctor | 0 | 2.37 | 10146 |
| mcp:project_query | 0 | 1.46 | 6272 |
| mcp:analyze | 0 | 4.47 | 8399 |
| update | 0 | 7.46 | 1203 |
| review | 3 | 2.92 | 14150 |
| update:revert | 0 | 6.53 | 1201 |
