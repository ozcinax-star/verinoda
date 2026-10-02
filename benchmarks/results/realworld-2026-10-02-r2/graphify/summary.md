# Real-world run 2026-10-02

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs.

Times in seconds, wall clock, one process at a time. Gold v1: the frozen facts; v2: the corrected checks added after review.

| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | clean after | gold v1 | gold v2 |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| Graphify-Labs/graphify (Python) | 950 | 103.6 | 51.6 | 2.8 / 10.3 | 0 | 0 | yes | 10/10 | - |

## Graphify-Labs/graphify v0.9.73

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:40:56.

Gold facts:

- hit  `def-build-from-json` (q)
- hit  `build-calls-build-from-json` (q)
- hit  `cluster-calls-partition` (q)
- hit  `cluster-reaches-leiden` (trace)
- hit  `cluster-calls-cohesion` (q)
- hit  `def-god-nodes` (q)
- hit  `def-extract-python` (q)
- hit  `main-calls-run-cli` (q)
- hit  `query-communities` (query): rank 1
- hit  `query-god-nodes` (query): rank 2

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.28 | 60 |
| scan | 0 | 103.58 | 2548 |
| gold:def-build-from-json | 0 | 2.35 | 1060 |
| gold:build-calls-build-from-json | 0 | 2.26 | 1572 |
| gold:cluster-calls-partition | 0 | 2.29 | 1534 |
| gold:cluster-reaches-leiden | 0 | 2.46 | 1522 |
| gold:cluster-calls-cohesion | 0 | 2.29 | 1560 |
| gold:def-god-nodes | 0 | 2.21 | 1044 |
| gold:def-extract-python | 0 | 2.31 | 1029 |
| gold:main-calls-run-cli | 0 | 2.35 | 1497 |
| gold:query-communities | 0 | 2.64 | 5730 |
| gold:query-god-nodes | 0 | 2.73 | 5745 |
| query1 | 0 | 2.83 | 5733 |
| query2 | 0 | 2.75 | 5763 |
| query3 | 0 | 3.30 | 5820 |
| analyze1 | 0 | 9.65 | 11580 |
| analyze2 | 0 | 10.89 | 12149 |
| trace | 0 found | 2.31 | 656 |
| q1 | 0 | 2.21 | 1135 |
| q2 | 0 | 2.17 | 17104 |
| check | 0 checked | 6.21 | 14095 |
| map:dependencies | 0 | 1.99 | 8439 |
| map:dataflow | 0 | 9.33 | 38611 |
| map:dead | 0 | 19.18 | 108994 |
| map:hotspots | 0 | 2.11 | 1299 |
| routes | 0 | 2.01 | 2634 |
| schema | 0 | 2.11 | 713 |
| taint | 0 | 9.48 | 1289 |
| doctor | 0 | 3.96 | 10593 |
| mcp:project_query | 0 | 2.30 | 6216 |
| mcp:analyze | 0 | 8.21 | 8264 |
| update | 0 | 51.63 | 2551 |
| review | 0 | 6.31 | 11878 |
| update:revert | 0 | 40.67 | 2550 |
