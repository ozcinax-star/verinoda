# Real-world run 2026-10-02

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs.

Times in seconds, wall clock, one process at a time. Gold v1: the frozen facts; v2: the corrected checks added after review.

| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | clean after | gold v1 | gold v2 |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| Graphify-Labs/graphify (Python) | 950 | 98.8 | 51.2 | 2.7 / 10.1 | 0 | 0 | yes | 10/10 | - |

## Graphify-Labs/graphify v0.9.73

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:55:34.

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
| init | 0 | 1.29 | 60 |
| scan | 0 | 98.82 | 2548 |
| gold:def-build-from-json | 0 | 2.23 | 1059 |
| gold:build-calls-build-from-json | 0 | 2.27 | 1572 |
| gold:cluster-calls-partition | 0 | 2.26 | 1533 |
| gold:cluster-reaches-leiden | 0 | 2.31 | 1522 |
| gold:cluster-calls-cohesion | 0 | 2.23 | 1559 |
| gold:def-god-nodes | 0 | 2.19 | 1044 |
| gold:def-extract-python | 0 | 2.20 | 1029 |
| gold:main-calls-run-cli | 0 | 2.24 | 1498 |
| gold:query-communities | 0 | 2.63 | 5730 |
| gold:query-god-nodes | 0 | 2.66 | 5745 |
| query1 | 0 | 2.69 | 5733 |
| query2 | 0 | 2.71 | 5763 |
| query3 | 0 | 3.28 | 5820 |
| analyze1 | 0 | 9.51 | 11580 |
| analyze2 | 0 | 10.79 | 12146 |
| trace | 0 found | 2.32 | 656 |
| q1 | 0 | 2.24 | 1160 |
| q2 | 0 | 2.17 | 17245 |
| check | 0 checked | 5.97 | 14095 |
| map:dependencies | 0 | 2.05 | 8412 |
| map:dataflow | 0 | 9.32 | 38611 |
| map:dead | 0 | 18.97 | 108994 |
| map:hotspots | 0 | 2.10 | 1299 |
| routes | 0 | 2.00 | 2634 |
| schema | 0 | 2.14 | 713 |
| taint | 0 | 8.67 | 1289 |
| doctor | 0 | 3.37 | 10593 |
| mcp:project_query | 0 | 2.37 | 6216 |
| mcp:analyze | 0 | 8.08 | 8264 |
| update | 0 | 51.22 | 2550 |
| review | 0 | 6.13 | 11878 |
| update:revert | 0 | 40.12 | 2550 |
