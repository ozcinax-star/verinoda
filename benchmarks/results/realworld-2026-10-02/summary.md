# Real-world run 2026-10-02

Verinoda 0.3.2 at 8d9ab97 and 1ab4a71, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs (the same verinoda/ code: the commits differ only in files outside verinoda/).

Times in seconds, wall clock, one process at a time. Gold v1: the frozen facts; v2: the corrected checks added after review.

| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | clean after | gold v1 | gold v2 |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| fastapi/full-stack-fastapi-template (Python + TypeScript) | 254 | 20.5 | 12.3 | 1.3 / 3.4 | 0 | 0 | yes | 7/10 | - |
| fastapi/sqlmodel (Python) | 526 | 35.1 | 18.3 | 1.6 / 2.8 | 0 | 0 | yes | 9/10 | - |
| expressjs/express (JavaScript) | 218 | 11.1 | 9.1 | 1.3 / 2.6 | 0 | 0 | yes | 4/10 | 2/2 |
| axios/axios (JavaScript + TypeScript) | 466 | 29.6 | 21.0 | 1.5 / 3.7 | 0 | 0 | yes | 7/10 | - |
| junegunn/fzf (Go) | 161 | 15.9 | 10.1 | 1.7 / 3.7 | 0 | 0 | yes | 8/10 | 3/3 |
| gin-gonic/gin (Go) | 130 | 12.2 | 8.1 | 1.6 / 2.8 | 0 | 0 | yes | 7/10 | 1/2 |
| sharkdp/bat (Rust) | 974 | 45.2 | 19.2 | 1.5 / 3.2 | 0 | 0 | yes | 8/10 | - |
| google/gson (Java) | 311 | 30.0 | 25.1 | 2.0 / 4.2 | 0 | 0 | yes | 9/10 | - |
| guzzle/guzzle (PHP) | 176 | 25.5 | 13.8 | 1.8 / 5.4 | 0 | 0 | yes | 8/10 | - |
| Graphify-Labs/graphify (Python) | 950 | 123.3 | 54.1 | 2.7 / 12.3 | 0 | 0 | yes | 10/10 | - |

## fastapi/full-stack-fastapi-template 0.12.0

Verinoda 0.3.2 at 8d9ab97, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:09:07.

Gold facts:

- hit  `def-authenticate` (q)
- hit  `login-calls-authenticate` (q)
- hit  `login-reaches-verify-password` (trace)
- hit  `users-create-calls-crud` (q)
- hit  `def-get-current-user` (q)
- MISS `route-login-access-token` (routes): route not in the table
- MISS `route-read-item` (routes): route not in the table
- hit  `schema-user-table` (schema)
- MISS `frontend-login-calls-sdk` (q): no row
- hit  `query-jwt` (query): rank 3

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 2.43 | 70 |
| scan | 0 | 20.53 | 1324 |
| gold:def-authenticate | 0 | 1.33 | 1052 |
| gold:login-calls-authenticate | 0 | 1.21 | 1630 |
| gold:login-reaches-verify-password | 0 | 1.25 | 1023 |
| gold:users-create-calls-crud | 0 | 1.18 | 1667 |
| gold:def-get-current-user | 0 | 1.19 | 1084 |
| gold:route-login-access-token | 0 | 1.19 | 4402 |
| gold:route-read-item | 0 | 1.18 | 4402 |
| gold:schema-user-table | 0 | 1.39 | 9333 |
| gold:frontend-login-calls-sdk | 1 | 1.18 | 796 |
| gold:query-jwt | 0 | 1.38 | 5705 |
| query1 | 0 | 1.60 | 5745 |
| query2 | 0 | 1.33 | 5715 |
| query3 | 0 | 1.33 | 5678 |
| analyze1 | 0 | 2.88 | 10498 |
| analyze2 | 0 | 3.86 | 12761 |
| trace | 0 found | 1.25 | 1023 |
| q1 | 0 | 1.24 | 1133 |
| q2 | 0 | 1.18 | 4495 |
| check | 0 checked | 3.54 | 2918 |
| map:dependencies | 0 | 1.17 | 9574 |
| map:dataflow | 0 | 1.88 | 9805 |
| map:dead | 0 | 2.39 | 94715 |
| map:hotspots | 0 | 1.49 | 1299 |
| routes | 0 | 1.25 | 4402 |
| schema | 0 | 1.40 | 9333 |
| taint | 3 | 1.62 | 3543 |
| doctor | 0 | 6.05 | 10640 |
| mcp:project_query | 0 | 1.68 | 6183 |
| mcp:analyze | 0 | 2.73 | 7549 |
| update | 0 | 12.32 | 1322 |
| review | 0 | 4.03 | 11212 |
| update:revert | 0 | 11.28 | 1320 |

## fastapi/sqlmodel 0.0.47

Verinoda 0.3.2 at 8d9ab97, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:10:50.

Gold facts:

- hit  `def-get-column-from-field` (q)
- hit  `metaclass-new-calls-column` (q)
- hit  `column-calls-satype` (trace)
- hit  `model-validate-calls-compat` (q)
- hit  `model-validate-reaches-compat` (trace)
- MISS `def-field-impl` (q): rows do not cite sqlmodel/main.py:388
- hit  `class-sqlmodel` (q)
- hit  `schema-hero-table` (schema)
- hit  `query-field-to-column` (query): rank 5
- hit  `query-session-exec` (query): rank 1

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.44 | 51 |
| scan | 0 | 35.11 | 1293 |
| gold:def-get-column-from-field | 0 | 1.34 | 1078 |
| gold:metaclass-new-calls-column | 0 | 1.36 | 1620 |
| gold:column-calls-satype | 0 | 1.26 | 727 |
| gold:model-validate-calls-compat | 0 | 1.27 | 1638 |
| gold:model-validate-reaches-compat | 0 | 1.29 | 729 |
| gold:def-field-impl | 0 | 1.32 | 1015 |
| gold:class-sqlmodel | 0 | 1.31 | 1013 |
| gold:schema-hero-table | 0 | 1.72 | 81381 |
| gold:query-field-to-column | 0 | 1.52 | 5737 |
| gold:query-session-exec | 0 | 1.50 | 5749 |
| query1 | 0 | 1.56 | 5695 |
| query2 | 0 | 1.52 | 5676 |
| query3 | 0 | 1.57 | 5737 |
| analyze1 | 0 | 2.86 | 10043 |
| analyze2 | 0 | 2.75 | 9224 |
| trace | 0 found | 1.31 | 729 |
| q1 | 0 | 1.30 | 1262 |
| q2 | 0 | 1.44 | 1422 |
| check | 0 checked | 6.25 | 18967 |
| map:dependencies | 0 | 1.27 | 10188 |
| map:dataflow | 0 | 2.31 | 105113 |
| map:dead | 0 | 3.63 | 117004 |
| map:hotspots | 0 | 1.68 | 1299 |
| routes | 0 | 1.47 | 214168 |
| schema | 0 | 1.73 | 81381 |
| taint | 0 | 2.32 | 1290 |
| doctor | 0 | 2.78 | 10152 |
| mcp:project_query | 0 | 1.43 | 5434 |
| mcp:analyze | 0 | 3.12 | 7643 |
| update | 0 | 18.33 | 1308 |
| review | 0 | 3.91 | 11185 |
| update:revert | 0 | 13.43 | 1307 |

## expressjs/express v5.2.1

Verinoda 0.3.2 at 1ab4a71, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:39:56.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-stringify` (q)
- MISS `json-calls-stringify` (q): no row
- MISS `json-calls-send` (q): no row
- MISS `sendfile-calls-helper` (trace): source resolved at lib/response.js:927, not lib/response.js:378
- MISS `render-reaches-tryrender` (trace): source not resolved (unresolved)
- hit  `def-create-application` (q)
- MISS `route-posts` (routes): route not in the table
- MISS `route-user-edit-put` (routes): route not in the table
- hit  `query-json-response` (query): rank 1
- hit  `query-accepts` (query): rank 6
- hit  `route-posts@v2` v2 (routes)
- hit  `route-user-edit-put@v2` v2 (routes)

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.33 | 52 |
| scan | 0 | 11.12 | 1207 |
| gold:def-stringify | 0 | 1.14 | 1028 |
| gold:json-calls-stringify | 1 | 1.18 | 775 |
| gold:json-calls-send | 1 | 1.19 | 770 |
| gold:sendfile-calls-helper | 2 | 1.86 | 462 |
| gold:render-reaches-tryrender | 2 | 4.34 | 774 |
| gold:def-create-application | 0 | 1.39 | 1020 |
| gold:route-posts | 0 | 1.49 | 247767 |
| gold:route-user-edit-put | 0 | 1.42 | 247767 |
| gold:query-json-response | 0 | 1.30 | 3786 |
| gold:query-accepts | 0 | 1.44 | 3741 |
| gold:route-posts@v2 | 0 | 1.58 | 247767 |
| gold:route-user-edit-put@v2 | 0 | 1.46 | 247767 |
| query1 | 0 | 1.28 | 4400 |
| query2 | 0 | 1.28 | 3707 |
| query3 | 0 | 1.27 | 4169 |
| analyze1 | 0 | 2.71 | 8693 |
| analyze2 | 0 | 2.47 | 9095 |
| trace | 2 unresolved | 1.53 | 771 |
| q1 | 0 | 1.17 | 1161 |
| q2 | 1 | 1.17 | 717 |
| map:dependencies | 0 | 1.22 | 8204 |
| map:dataflow | 0 | 1.47 | 1449 |
| map:dead | 0 | 1.81 | 59042 |
| map:hotspots | 0 | 1.46 | 1299 |
| routes | 0 | 1.42 | 247767 |
| schema | 0 | 1.31 | 713 |
| doctor | 0 | 2.50 | 10143 |
| mcp:project_query | 0 | 1.22 | 5081 |
| mcp:analyze | 0 | 2.93 | 6258 |
| update | 0 | 9.13 | 1226 |
| review | 0 | 3.59 | 11173 |
| update:revert | 0 | 9.05 | 1224 |

## axios/axios v1.20.0

Verinoda 0.3.2 at 8d9ab97, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:14:22.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-dispatch-request` (q)
- MISS `request-calls-private` (q): no row
- MISS `private-request-merges-config` (q): no row
- hit  `dispatch-checks-cancel` (trace)
- MISS `request-reaches-adapter` (trace): source not resolved (unresolved)
- hit  `create-instance-binds` (q)
- hit  `def-merge-config` (q)
- hit  `def-get-adapter` (q)
- hit  `query-interceptors` (query): rank 1
- hit  `query-http-adapter` (query): rank 1

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.31 | 46 |
| scan | 0 | 29.63 | 1184 |
| gold:def-dispatch-request | 0 | 1.61 | 1052 |
| gold:request-calls-private | 1 | 1.60 | 780 |
| gold:private-request-merges-config | 1 | 1.44 | 749 |
| gold:dispatch-checks-cancel | 0 | 1.40 | 841 |
| gold:request-reaches-adapter | 2 | 2.07 | 1401 |
| gold:create-instance-binds | 0 | 1.35 | 1464 |
| gold:def-merge-config | 0 | 1.28 | 1065 |
| gold:def-get-adapter | 0 | 1.28 | 1065 |
| gold:query-interceptors | 0 | 1.51 | 5511 |
| gold:query-http-adapter | 0 | 1.58 | 4600 |
| query1 | 0 | 1.55 | 5597 |
| query2 | 0 | 1.62 | 5707 |
| query3 | 0 | 1.44 | 5361 |
| analyze1 | 0 | 4.48 | 9108 |
| analyze2 | 0 | 2.95 | 9038 |
| trace | 2 no directed path | 1.37 | 685 |
| q1 | 0 | 1.37 | 1218 |
| q2 | 0 | 1.34 | 3981 |
| map:dependencies | 0 | 1.28 | 9352 |
| map:dataflow | 0 | 2.74 | 2682 |
| map:dead | 0 | 3.30 | 106066 |
| map:hotspots | 0 | 1.62 | 1299 |
| routes | 0 | 1.60 | 83665 |
| schema | 0 | 1.55 | 713 |
| doctor | 0 | 2.77 | 10352 |
| mcp:project_query | 0 | 1.47 | 5435 |
| mcp:analyze | 0 | 4.81 | 7556 |
| update | 0 | 20.97 | 1567 |
| review | 0 | 4.63 | 11957 |
| update:revert | 0 | 18.11 | 1216 |

## junegunn/fzf v0.74.4

Verinoda 0.3.2 at 8d9ab97, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:16:30.

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
| init | 0 | 1.46 | 47 |
| scan | 0 | 15.86 | 1188 |
| gold:def-run | 0 | 1.30 | 985 |
| gold:main-calls-run | 2 | 1.27 | 313 |
| gold:parse-calls-parse | 0 | 1.33 | 1513 |
| gold:run-calls-postprocess | 0 | 1.33 | 1582 |
| gold:run-calls-newmatcher | 0 | 1.36 | 615 |
| gold:matcher-loop-scan | 0 | 1.37 | 1471 |
| gold:def-fuzzymatchv2 | 0 | 1.35 | 1009 |
| gold:pattern-matchitem | 0 | 1.30 | 1539 |
| gold:query-fuzzy-algo | 0 | 1.55 | 5677 |
| gold:query-options | 0 | 1.53 | 5236 |
| gold:main-calls-run@v2 | 0 | 1.31 | 484 |
| gold:run-calls-newmatcher@v2 | 0 | 1.37 | 542 |
| gold:pattern-matchitem@v2 | 0 | 1.32 | 1539 |
| query1 | 0 | 1.66 | 5696 |
| query2 | 0 | 1.68 | 5655 |
| query3 | 0 | 1.41 | 5630 |
| analyze1 | 0 | 4.79 | 10033 |
| analyze2 | 0 | 2.52 | 9576 |
| trace | 0 found | 1.27 | 683 |
| q1 | 0 | 1.36 | 1177 |
| q2 | 0 | 1.29 | 1306 |
| map:dependencies | 0 | 1.20 | 7868 |
| map:dataflow | 0 | 2.45 | 1509 |
| map:dead | 0 | 3.04 | 140635 |
| map:hotspots | 0 | 1.49 | 1299 |
| routes | 0 | 1.14 | 252 |
| schema | 0 | 1.36 | 713 |
| doctor | 0 | 2.55 | 10157 |
| mcp:project_query | 0 | 1.56 | 6272 |
| mcp:analyze | 0 | 4.78 | 8401 |
| update | 0 | 10.13 | 1203 |
| review | 3 | 3.74 | 14150 |
| update:revert | 0 | 7.34 | 1202 |

## gin-gonic/gin v1.12.0

Verinoda 0.3.2 at 8d9ab97, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:18:02.

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
| init | 0 | 1.32 | 48 |
| scan | 0 | 12.23 | 1190 |
| gold:def-default | 0 | 1.25 | 983 |
| gold:default-calls-new | 0 | 1.29 | 1363 |
| gold:default-calls-recovery | 2 | 1.32 | 935 |
| gold:servehttp-calls-handle | 0 | 1.29 | 1569 |
| gold:request-reaches-tree | 2 | 1.30 | 521 |
| gold:get-registers-route | 1 | 1.30 | 803 |
| gold:recovery-calls-writer | 0 | 1.37 | 1475 |
| gold:context-json-method | 0 | 1.32 | 1023 |
| gold:query-radix-tree | 0 | 1.59 | 5279 |
| gold:query-recovery | 0 | 1.40 | 5201 |
| gold:default-calls-recovery@v2 | 0 | 1.35 | 513 |
| gold:request-reaches-tree@v2 | 2 | 1.38 | 521 |
| query1 | 0 | 1.43 | 5687 |
| query2 | 0 | 1.62 | 5311 |
| query3 | 0 | 1.59 | 5272 |
| analyze1 | 0 | 2.88 | 11642 |
| analyze2 | 0 | 2.71 | 9086 |
| trace | 0 found | 1.29 | 719 |
| q1 | 0 | 1.28 | 1180 |
| q2 | 1 | 1.27 | 718 |
| map:dependencies | 0 | 1.21 | 8392 |
| map:dataflow | 0 | 1.77 | 3292 |
| map:dead | 0 | 2.48 | 148766 |
| map:hotspots | 0 | 1.53 | 1299 |
| routes | 0 | 1.20 | 252 |
| schema | 0 | 1.55 | 713 |
| doctor | 0 | 2.58 | 9932 |
| mcp:project_query | 0 | 1.35 | 6186 |
| mcp:analyze | 0 | 2.98 | 8737 |
| update | 0 | 8.09 | 1198 |
| review | 3 | 3.62 | 14691 |
| update:revert | 0 | 6.58 | 1199 |

## sharkdp/bat v0.26.1

Verinoda 0.3.2 at 8d9ab97, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:19:21.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-run-controller` (q)
- MISS `run-controller-new` (q): no row
- hit  `controller-run-delegates` (q)
- hit  `main-reaches-run-controller` (trace)
- hit  `run-reaches-print-file-ranges` (trace)
- hit  `def-interactive-print-line` (q)
- hit  `struct-controller` (q)
- MISS `print-file-calls-header` (q): no row
- hit  `query-pager` (query): rank 4
- hit  `query-highlight` (query): rank 2

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.34 | 46 |
| scan | 0 | 45.25 | 1341 |
| gold:def-run-controller | 0 | 1.54 | 1026 |
| gold:run-controller-new | 1 | 1.40 | 782 |
| gold:controller-run-delegates | 0 | 1.33 | 1644 |
| gold:main-reaches-run-controller | 0 | 1.37 | 826 |
| gold:run-reaches-print-file-ranges | 0 | 1.35 | 1442 |
| gold:def-interactive-print-line | 0 | 1.34 | 1692 |
| gold:struct-controller | 0 | 1.32 | 987 |
| gold:print-file-calls-header | 1 | 1.35 | 787 |
| gold:query-pager | 0 | 1.59 | 5588 |
| gold:query-highlight | 0 | 1.59 | 5720 |
| query1 | 0 | 1.54 | 5644 |
| query2 | 0 | 1.51 | 5593 |
| query3 | 0 | 1.55 | 5716 |
| analyze1 | 0 | 3.07 | 10712 |
| analyze2 | 0 | 3.36 | 11678 |
| trace | 2 no directed path | 1.38 | 553 |
| q1 | 0 | 1.38 | 1232 |
| q2 | 1 | 1.37 | 724 |
| map:dependencies | 0 | 1.28 | 7801 |
| map:dataflow | 0 | 1.95 | 4453 |
| map:dead | 0 | 4.69 | 77105 |
| map:hotspots | 0 | 1.69 | 1299 |
| routes | 0 | 1.41 | 252 |
| schema | 0 | 1.50 | 713 |
| doctor | 0 | 2.90 | 13988 |
| mcp:project_query | 0 | 1.52 | 6220 |
| mcp:analyze | 0 | 3.65 | 8194 |
| update | 0 | 19.20 | 1360 |
| review | 3 | 4.82 | 12775 |
| update:revert | 0 | 17.27 | 1361 |

## google/gson gson-parent-2.14.0

Verinoda 0.3.2 at 8d9ab97, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:21:39.

Gold facts:

- MISS `def-dopeek` (q): rows do not cite gson/src/main/java/com/google/gson/stream/JsonReader.java:582
- hit  `peek-calls-dopeek` (q)
- hit  `tojson-calls-getadapter` (q)
- hit  `getadapter-calls-factory-create` (q)
- hit  `fromjson-calls-getadapter` (q)
- hit  `parsestring-reaches-streams` (trace)
- hit  `boundfields-calls-create` (q)
- hit  `class-reflective-factory` (q)
- hit  `query-reflective` (query): rank 1
- hit  `query-json-reader` (query): rank 1

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.39 | 46 |
| scan | 0 | 29.99 | 1185 |
| gold:def-dopeek | 0 | 1.62 | 1212 |
| gold:peek-calls-dopeek | 0 | 1.63 | 1951 |
| gold:tojson-calls-getadapter | 0 | 1.70 | 3037 |
| gold:getadapter-calls-factory-create | 0 | 1.62 | 2009 |
| gold:fromjson-calls-getadapter | 0 | 1.66 | 3065 |
| gold:parsestring-reaches-streams | 0 | 1.73 | 2327 |
| gold:boundfields-calls-create | 0 | 1.66 | 2293 |
| gold:class-reflective-factory | 0 | 1.63 | 1254 |
| gold:query-reflective | 0 | 2.29 | 5666 |
| gold:query-json-reader | 0 | 1.98 | 5705 |
| query1 | 0 | 1.98 | 5745 |
| query2 | 0 | 2.04 | 5740 |
| query3 | 0 | 1.97 | 5737 |
| analyze1 | 0 | 4.66 | 11280 |
| analyze2 | 0 | 3.70 | 8911 |
| trace | 0 found | 1.66 | 1033 |
| q1 | 0 | 1.61 | 1351 |
| q2 | 0 | 1.66 | 22683 |
| check | 0 checked | 1.71 | 35610 |
| map:dependencies | 0 | 1.60 | 12846 |
| map:dataflow | 0 | 2.89 | 2545 |
| map:dead | 0 | 4.11 | 157008 |
| map:hotspots | 0 | 1.82 | 1299 |
| routes | 0 | 1.59 | 252 |
| schema | 0 | 1.78 | 713 |
| doctor | 0 | 2.78 | 9939 |
| mcp:project_query | 0 | 2.00 | 5627 |
| mcp:analyze | 0 | 4.84 | 9159 |
| update | 0 | 25.11 | 1232 |
| review | 3 | 7.53 | 76798 |
| update:revert | 0 | 21.44 | 1232 |

## guzzle/guzzle 8.2.0

Verinoda 0.3.2 at 8d9ab97, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:24:07.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-transfer` (q)
- hit  `sendasync-calls-transfer` (q)
- hit  `send-calls-sendasync` (q)
- hit  `requestasync-calls-transfer` (q)
- hit  `send-reaches-apply-options` (trace)
- MISS `stack-create-chooses-handler` (q): no row
- MISS `stack-create-pushes-redirect` (q): no row
- hit  `def-choose-handler` (q)
- hit  `query-redirects` (query): rank 1
- hit  `query-cookies` (query): rank 3

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.34 | 48 |
| scan | 0 | 25.48 | 1193 |
| gold:def-transfer | 0 | 1.34 | 1046 |
| gold:sendasync-calls-transfer | 0 | 1.40 | 1553 |
| gold:send-calls-sendasync | 0 | 1.40 | 1527 |
| gold:requestasync-calls-transfer | 0 | 1.38 | 1570 |
| gold:send-reaches-apply-options | 0 | 1.41 | 1031 |
| gold:stack-create-chooses-handler | 1 | 1.38 | 787 |
| gold:stack-create-pushes-redirect | 1 | 1.36 | 781 |
| gold:def-choose-handler | 0 | 1.37 | 1027 |
| gold:query-redirects | 0 | 2.84 | 5685 |
| gold:query-cookies | 0 | 1.59 | 5694 |
| query1 | 0 | 1.77 | 5741 |
| query2 | 0 | 1.59 | 5680 |
| query3 | 0 | 2.65 | 5735 |
| analyze1 | 0 | 5.43 | 13009 |
| analyze2 | 0 | 5.37 | 9281 |
| trace | 0 found | 1.36 | 850 |
| q1 | 0 | 1.32 | 1282 |
| q2 | 0 | 2.77 | 2010 |
| map:dependencies | 0 | 1.26 | 9592 |
| map:dataflow | 0 | 2.29 | 1234 |
| map:dead | 0 | 4.14 | 100702 |
| map:hotspots | 0 | 1.84 | 1299 |
| routes | 0 | 1.23 | 252 |
| schema | 0 | 1.50 | 713 |
| doctor | 0 | 4.26 | 9963 |
| mcp:project_query | 0 | 1.63 | 6171 |
| mcp:analyze | 0 | 5.09 | 10007 |
| update | 0 | 13.78 | 1463 |
| review | 3 | 4.62 | 12597 |
| update:revert | 0 | 11.29 | 1209 |

## Graphify-Labs/graphify v0.9.73

Verinoda 0.3.2 at 8d9ab97, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T00:26:17.

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
| scan | 0 | 123.31 | 1615 |
| gold:def-build-from-json | 0 | 2.16 | 1060 |
| gold:build-calls-build-from-json | 0 | 2.33 | 1572 |
| gold:cluster-calls-partition | 0 | 2.26 | 1534 |
| gold:cluster-reaches-leiden | 0 | 3.16 | 1522 |
| gold:cluster-calls-cohesion | 0 | 3.12 | 1560 |
| gold:def-god-nodes | 0 | 2.35 | 1044 |
| gold:def-extract-python | 0 | 2.15 | 1029 |
| gold:main-calls-run-cli | 0 | 2.21 | 1498 |
| gold:query-communities | 0 | 2.48 | 5730 |
| gold:query-god-nodes | 0 | 2.58 | 5745 |
| query1 | 0 | 2.56 | 5733 |
| query2 | 0 | 2.70 | 5763 |
| query3 | 0 | 3.43 | 5820 |
| analyze1 | 0 | 11.82 | 11523 |
| analyze2 | 0 | 12.76 | 12217 |
| trace | 0 found | 2.45 | 656 |
| q1 | 0 | 2.43 | 1145 |
| q2 | 0 | 2.28 | 17794 |
| check | 0 checked | 8.87 | 14095 |
| map:dependencies | 0 | 2.64 | 8412 |
| map:dataflow | 0 | 10.22 | 38611 |
| map:dead | 0 | 21.57 | 108824 |
| map:hotspots | 0 | 2.36 | 1299 |
| routes | 0 | 2.07 | 2055 |
| schema | 0 | 2.21 | 713 |
| taint | 0 | 9.33 | 1289 |
| doctor | 0 | 3.53 | 10558 |
| mcp:project_query | 0 | 2.48 | 6216 |
| mcp:analyze | 0 | 10.09 | 8392 |
| update | 0 | 54.07 | 1617 |
| review | 0 | 7.40 | 11878 |
| update:revert | 0 | 41.36 | 1617 |
