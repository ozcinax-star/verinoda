# Real-world run 2026-10-02

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs.

Times in seconds, wall clock, one process at a time. Gold v1: the frozen facts; v2: the corrected checks added after review.

| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | clean after | gold v1 | gold v2 |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| fastapi/full-stack-fastapi-template (Python + TypeScript) | 254 | 18.2 | 11.3 | 1.4 / 3.5 | 0 | 0 | yes | 10/10 | - |
| fastapi/sqlmodel (Python) | 526 | 28.0 | 17.2 | 1.5 / 2.7 | 0 | 0 | yes | 10/10 | - |
| expressjs/express (JavaScript) | 218 | 10.4 | 8.4 | 1.3 / 2.7 | 0 | 0 | yes | 7/10 | 2/2 |
| axios/axios (JavaScript + TypeScript) | 466 | 24.0 | 18.6 | 1.5 / 3.6 | 0 | 0 | yes | 9/10 | - |
| junegunn/fzf (Go) | 161 | 15.7 | 8.4 | 1.6 / 3.5 | 0 | 0 | yes | 8/10 | 3/3 |
| gin-gonic/gin (Go) | 130 | 11.6 | 7.8 | 1.4 / 2.7 | 0 | 0 | yes | 7/10 | 1/2 |
| sharkdp/bat (Rust) | 974 | 35.6 | 20.0 | 1.6 / 3.2 | 0 | 0 | yes | 8/10 | - |
| google/gson (Java) | 311 | 27.4 | 24.8 | 1.9 / 4.0 | 0 | 0 | yes | 10/10 | - |
| guzzle/guzzle (PHP) | 176 | 23.4 | 13.1 | 1.6 / 4.7 | 0 | 0 | yes | 8/10 | - |

## fastapi/full-stack-fastapi-template 0.12.0

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:25:14.

Gold facts:

- hit  `def-authenticate` (q)
- hit  `login-calls-authenticate` (q)
- hit  `login-reaches-verify-password` (trace)
- hit  `users-create-calls-crud` (q)
- hit  `def-get-current-user` (q)
- hit  `route-login-access-token` (routes)
- hit  `route-read-item` (routes)
- hit  `schema-user-table` (schema)
- hit  `frontend-login-calls-sdk` (q)
- hit  `query-jwt` (query): rank 3

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 2.82 | 70 |
| scan | 0 | 18.16 | 1325 |
| gold:def-authenticate | 0 | 1.28 | 1051 |
| gold:login-calls-authenticate | 0 | 1.24 | 1631 |
| gold:login-reaches-verify-password | 0 | 1.41 | 1023 |
| gold:users-create-calls-crud | 0 | 1.21 | 1668 |
| gold:def-get-current-user | 0 | 1.20 | 1084 |
| gold:route-login-access-token | 0 | 1.21 | 8420 |
| gold:route-read-item | 0 | 1.32 | 8420 |
| gold:schema-user-table | 0 | 1.39 | 9333 |
| gold:frontend-login-calls-sdk | 0 | 1.56 | 1720 |
| gold:query-jwt | 0 | 1.46 | 5666 |
| query1 | 0 | 1.42 | 5734 |
| query2 | 0 | 1.39 | 5716 |
| query3 | 0 | 1.38 | 5678 |
| analyze1 | 0 | 3.08 | 10515 |
| analyze2 | 0 | 3.84 | 12759 |
| trace | 0 found | 1.20 | 1023 |
| q1 | 0 | 1.22 | 1210 |
| q2 | 0 | 1.21 | 4495 |
| check | 0 checked | 3.27 | 2918 |
| map:dependencies | 0 | 1.23 | 9593 |
| map:dataflow | 0 | 2.00 | 10018 |
| map:dead | 0 | 2.44 | 94866 |
| map:hotspots | 0 | 1.47 | 1299 |
| routes | 0 | 1.21 | 8420 |
| schema | 0 | 1.33 | 9333 |
| taint | 3 | 1.65 | 3543 |
| doctor | 0 | 2.76 | 10831 |
| mcp:project_query | 0 | 1.49 | 6250 |
| mcp:analyze | 0 | 2.55 | 7570 |
| update | 0 | 11.33 | 1322 |
| review | 0 | 3.65 | 11212 |
| update:revert | 0 | 10.79 | 1322 |

## fastapi/sqlmodel 0.0.47

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:26:50.

Gold facts:

- hit  `def-get-column-from-field` (q)
- hit  `metaclass-new-calls-column` (q)
- hit  `column-calls-satype` (trace)
- hit  `model-validate-calls-compat` (q)
- hit  `model-validate-reaches-compat` (trace)
- hit  `def-field-impl` (q)
- hit  `class-sqlmodel` (q)
- hit  `schema-hero-table` (schema)
- hit  `query-field-to-column` (query): rank 5
- hit  `query-session-exec` (query): rank 1

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.27 | 51 |
| scan | 0 | 28.01 | 1294 |
| gold:def-get-column-from-field | 0 | 1.32 | 1078 |
| gold:metaclass-new-calls-column | 0 | 1.38 | 1620 |
| gold:column-calls-satype | 0 | 1.34 | 727 |
| gold:model-validate-calls-compat | 0 | 1.30 | 1638 |
| gold:model-validate-reaches-compat | 0 | 1.38 | 729 |
| gold:def-field-impl | 0 | 1.32 | 1015 |
| gold:class-sqlmodel | 0 | 1.40 | 1013 |
| gold:schema-hero-table | 0 | 1.71 | 81381 |
| gold:query-field-to-column | 0 | 1.58 | 5739 |
| gold:query-session-exec | 0 | 1.52 | 5765 |
| query1 | 0 | 1.54 | 5695 |
| query2 | 0 | 1.56 | 5676 |
| query3 | 0 | 1.51 | 5712 |
| analyze1 | 0 | 2.73 | 9941 |
| analyze2 | 0 | 2.65 | 9290 |
| trace | 0 found | 1.33 | 729 |
| q1 | 0 | 1.29 | 1127 |
| q2 | 0 | 1.35 | 1422 |
| check | 0 checked | 5.11 | 18967 |
| map:dependencies | 0 | 1.27 | 10188 |
| map:dataflow | 0 | 2.23 | 105113 |
| map:dead | 0 | 3.58 | 117038 |
| map:hotspots | 0 | 1.51 | 1299 |
| routes | 0 | 1.46 | 51570 |
| schema | 0 | 1.72 | 81381 |
| taint | 0 | 2.29 | 1290 |
| doctor | 0 | 2.67 | 10343 |
| mcp:project_query | 0 | 1.43 | 5434 |
| mcp:analyze | 0 | 2.93 | 7540 |
| update | 0 | 17.21 | 1309 |
| review | 0 | 3.23 | 11185 |
| update:revert | 0 | 12.47 | 1304 |

## expressjs/express v5.2.1

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:28:47.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-stringify` (q)
- hit  `json-calls-stringify` (q)
- hit  `json-calls-send` (q)
- hit  `sendfile-calls-helper` (trace)
- MISS `render-reaches-tryrender` (trace): no path (no directed path)
- hit  `def-create-application` (q)
- MISS `route-posts` (routes): route not in the table (same path: handler examples_route_separation_post_list)
- MISS `route-user-edit-put` (routes): route not in the table (same path: handler examples_route_middleware_index; handler examples_route_separation_user_edit; handler examples_route_separation_user_update)
- hit  `query-json-response` (query): rank 1
- hit  `query-accepts` (query): rank 6
- hit  `route-posts@v2` v2 (routes)
- hit  `route-user-edit-put@v2` v2 (routes)

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.37 | 52 |
| scan | 0 | 10.41 | 1208 |
| gold:def-stringify | 0 | 1.15 | 1028 |
| gold:json-calls-stringify | 0 | 1.16 | 1501 |
| gold:json-calls-send | 0 | 1.16 | 1498 |
| gold:sendfile-calls-helper | 0 | 1.30 | 781 |
| gold:render-reaches-tryrender | 2 | 1.21 | 424 |
| gold:def-create-application | 0 | 1.19 | 1020 |
| gold:route-posts | 0 | 1.52 | 73704 |
| gold:route-user-edit-put | 0 | 1.44 | 73704 |
| gold:query-json-response | 0 | 1.34 | 4281 |
| gold:query-accepts | 0 | 1.45 | 3795 |
| gold:route-posts@v2 | 0 | 1.42 | 73704 |
| gold:route-user-edit-put@v2 | 0 | 1.41 | 73704 |
| query1 | 0 | 1.33 | 5624 |
| query2 | 0 | 1.33 | 3957 |
| query3 | 0 | 1.33 | 4470 |
| analyze1 | 0 | 2.94 | 12401 |
| analyze2 | 0 | 2.49 | 10364 |
| trace | 0 found | 1.17 | 588 |
| q1 | 0 | 1.14 | 1146 |
| q2 | 0 | 1.21 | 2541 |
| map:dependencies | 0 | 1.20 | 8220 |
| map:dataflow | 0 | 1.39 | 4733 |
| map:dead | 0 | 1.82 | 78558 |
| map:hotspots | 0 | 1.32 | 1299 |
| routes | 0 | 1.41 | 73704 |
| schema | 0 | 1.23 | 713 |
| doctor | 0 | 2.47 | 10334 |
| mcp:project_query | 0 | 1.23 | 5729 |
| mcp:analyze | 0 | 2.77 | 8911 |
| update | 0 | 8.42 | 2429 |
| review | 0 | 2.91 | 11174 |
| update:revert | 0 | 8.48 | 1225 |

## axios/axios v1.20.0

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:30:02.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-dispatch-request` (q)
- hit  `request-calls-private` (q)
- hit  `private-request-merges-config` (q)
- hit  `dispatch-checks-cancel` (trace)
- MISS `request-reaches-adapter` (trace): no path (no directed path)
- hit  `create-instance-binds` (q)
- hit  `def-merge-config` (q)
- hit  `def-get-adapter` (q)
- hit  `query-interceptors` (query): rank 1
- hit  `query-http-adapter` (query): rank 1

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.26 | 46 |
| scan | 0 | 23.98 | 1185 |
| gold:def-dispatch-request | 0 | 1.27 | 1052 |
| gold:request-calls-private | 0 | 1.28 | 1576 |
| gold:private-request-merges-config | 0 | 1.31 | 1556 |
| gold:dispatch-checks-cancel | 0 | 1.34 | 841 |
| gold:request-reaches-adapter | 2 | 1.34 | 591 |
| gold:create-instance-binds | 0 | 1.39 | 1464 |
| gold:def-merge-config | 0 | 1.29 | 1064 |
| gold:def-get-adapter | 0 | 1.29 | 1065 |
| gold:query-interceptors | 0 | 1.54 | 5398 |
| gold:query-http-adapter | 0 | 1.54 | 4594 |
| query1 | 0 | 1.49 | 5533 |
| query2 | 0 | 1.56 | 5691 |
| query3 | 0 | 1.46 | 5382 |
| analyze1 | 0 | 4.29 | 9623 |
| analyze2 | 0 | 2.88 | 9034 |
| trace | 2 no directed path | 1.43 | 518 |
| q1 | 0 | 1.27 | 1171 |
| q2 | 0 | 1.29 | 3998 |
| map:dependencies | 0 | 1.24 | 9362 |
| map:dataflow | 0 | 2.57 | 2682 |
| map:dead | 0 | 3.27 | 104280 |
| map:hotspots | 0 | 1.44 | 1299 |
| routes | 0 | 1.57 | 36434 |
| schema | 0 | 1.34 | 713 |
| doctor | 0 | 2.66 | 10543 |
| mcp:project_query | 0 | 1.43 | 5448 |
| mcp:analyze | 0 | 4.55 | 7958 |
| update | 0 | 18.61 | 1568 |
| review | 0 | 3.70 | 11957 |
| update:revert | 0 | 16.86 | 1216 |

## junegunn/fzf v0.74.4

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:31:57.

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
| init | 0 | 1.28 | 47 |
| scan | 0 | 15.69 | 1187 |
| gold:def-run | 0 | 1.24 | 985 |
| gold:main-calls-run | 2 | 1.27 | 313 |
| gold:parse-calls-parse | 0 | 1.24 | 1514 |
| gold:run-calls-postprocess | 0 | 1.25 | 1582 |
| gold:run-calls-newmatcher | 0 | 1.31 | 615 |
| gold:matcher-loop-scan | 0 | 1.23 | 1471 |
| gold:def-fuzzymatchv2 | 0 | 1.22 | 1009 |
| gold:pattern-matchitem | 0 | 1.27 | 1539 |
| gold:query-fuzzy-algo | 0 | 1.50 | 5677 |
| gold:query-options | 0 | 1.50 | 5236 |
| gold:main-calls-run@v2 | 0 | 1.28 | 484 |
| gold:run-calls-newmatcher@v2 | 0 | 1.33 | 542 |
| gold:pattern-matchitem@v2 | 0 | 1.22 | 1539 |
| query1 | 0 | 1.59 | 5696 |
| query2 | 0 | 1.64 | 5655 |
| query3 | 0 | 1.39 | 5630 |
| analyze1 | 0 | 4.50 | 10034 |
| analyze2 | 0 | 2.48 | 9574 |
| trace | 0 found | 1.27 | 683 |
| q1 | 0 | 1.23 | 1177 |
| q2 | 0 | 1.26 | 1306 |
| map:dependencies | 0 | 1.20 | 7868 |
| map:dataflow | 0 | 2.44 | 1509 |
| map:dead | 0 | 2.82 | 142220 |
| map:hotspots | 0 | 1.45 | 1299 |
| routes | 0 | 1.17 | 491 |
| schema | 0 | 1.29 | 713 |
| doctor | 0 | 2.47 | 10348 |
| mcp:project_query | 0 | 1.53 | 6272 |
| mcp:analyze | 0 | 4.62 | 8399 |
| update | 0 | 8.37 | 1203 |
| review | 3 | 3.03 | 14150 |
| update:revert | 0 | 6.84 | 1200 |

## gin-gonic/gin v1.12.0

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:33:23.

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
| init | 0 | 1.30 | 48 |
| scan | 0 | 11.62 | 1191 |
| gold:def-default | 0 | 1.24 | 983 |
| gold:default-calls-new | 0 | 1.24 | 1363 |
| gold:default-calls-recovery | 2 | 1.27 | 935 |
| gold:servehttp-calls-handle | 0 | 1.24 | 1569 |
| gold:request-reaches-tree | 2 | 1.30 | 521 |
| gold:get-registers-route | 1 | 1.24 | 803 |
| gold:recovery-calls-writer | 0 | 1.23 | 1475 |
| gold:context-json-method | 0 | 1.23 | 1023 |
| gold:query-radix-tree | 0 | 1.48 | 5279 |
| gold:query-recovery | 0 | 1.36 | 5201 |
| gold:default-calls-recovery@v2 | 0 | 1.25 | 513 |
| gold:request-reaches-tree@v2 | 2 | 1.29 | 521 |
| query1 | 0 | 1.39 | 5687 |
| query2 | 0 | 1.49 | 5311 |
| query3 | 0 | 1.42 | 5272 |
| analyze1 | 0 | 2.81 | 11643 |
| analyze2 | 0 | 2.55 | 9086 |
| trace | 0 found | 1.29 | 719 |
| q1 | 0 | 1.21 | 1180 |
| q2 | 1 | 1.24 | 718 |
| map:dependencies | 0 | 1.21 | 8392 |
| map:dataflow | 0 | 1.81 | 3292 |
| map:dead | 0 | 2.40 | 149895 |
| map:hotspots | 0 | 1.37 | 1299 |
| routes | 0 | 1.13 | 491 |
| schema | 0 | 1.29 | 713 |
| doctor | 0 | 2.48 | 10123 |
| mcp:project_query | 0 | 1.31 | 6186 |
| mcp:analyze | 0 | 2.80 | 8737 |
| update | 0 | 7.75 | 1199 |
| review | 3 | 3.24 | 14691 |
| update:revert | 0 | 6.15 | 1199 |

## sharkdp/bat v0.26.1

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:34:38.

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
| init | 0 | 1.31 | 46 |
| scan | 0 | 35.58 | 2182 |
| gold:def-run-controller | 0 | 1.50 | 1026 |
| gold:run-controller-new | 1 | 1.42 | 781 |
| gold:controller-run-delegates | 0 | 1.42 | 1644 |
| gold:main-reaches-run-controller | 0 | 1.43 | 826 |
| gold:run-reaches-print-file-ranges | 0 | 1.45 | 1442 |
| gold:def-interactive-print-line | 0 | 1.41 | 1692 |
| gold:struct-controller | 0 | 1.37 | 988 |
| gold:print-file-calls-header | 1 | 1.38 | 787 |
| gold:query-pager | 0 | 1.64 | 5588 |
| gold:query-highlight | 0 | 1.63 | 5580 |
| query1 | 0 | 1.56 | 5644 |
| query2 | 0 | 1.54 | 5592 |
| query3 | 0 | 1.56 | 5716 |
| analyze1 | 0 | 3.18 | 10728 |
| analyze2 | 0 | 3.23 | 11675 |
| trace | 2 no directed path | 1.43 | 553 |
| q1 | 0 | 1.39 | 1240 |
| q2 | 1 | 1.42 | 724 |
| map:dependencies | 0 | 1.33 | 7854 |
| map:dataflow | 0 | 1.99 | 4453 |
| map:dead | 0 | 4.78 | 77105 |
| map:hotspots | 0 | 1.51 | 1299 |
| routes | 0 | 1.38 | 491 |
| schema | 0 | 1.47 | 713 |
| doctor | 0 | 2.86 | 14099 |
| mcp:project_query | 0 | 1.53 | 6215 |
| mcp:analyze | 0 | 3.35 | 8212 |
| update | 0 | 19.98 | 2203 |
| review | 3 | 4.41 | 12775 |
| update:revert | 0 | 16.53 | 2203 |

## google/gson gson-parent-2.14.0

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:36:47.

Gold facts:

- hit  `def-dopeek` (q)
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
| init | 0 | 1.31 | 46 |
| scan | 0 | 27.43 | 1185 |
| gold:def-dopeek | 0 | 1.59 | 1212 |
| gold:peek-calls-dopeek | 0 | 1.61 | 1951 |
| gold:tojson-calls-getadapter | 0 | 1.62 | 3037 |
| gold:getadapter-calls-factory-create | 0 | 1.63 | 2009 |
| gold:fromjson-calls-getadapter | 0 | 1.59 | 3065 |
| gold:parsestring-reaches-streams | 0 | 1.67 | 2327 |
| gold:boundfields-calls-create | 0 | 1.60 | 2293 |
| gold:class-reflective-factory | 0 | 1.57 | 1254 |
| gold:query-reflective | 0 | 2.02 | 5666 |
| gold:query-json-reader | 0 | 1.93 | 5705 |
| query1 | 0 | 2.02 | 5745 |
| query2 | 0 | 1.95 | 5740 |
| query3 | 0 | 1.87 | 5737 |
| analyze1 | 0 | 4.58 | 11279 |
| analyze2 | 0 | 3.46 | 8910 |
| trace | 0 found | 1.73 | 1033 |
| q1 | 0 | 1.65 | 1351 |
| q2 | 0 | 1.66 | 22683 |
| check | 0 checked | 1.68 | 35611 |
| map:dependencies | 0 | 1.62 | 12846 |
| map:dataflow | 0 | 2.80 | 2545 |
| map:dead | 0 | 4.00 | 162293 |
| map:hotspots | 0 | 1.70 | 1299 |
| routes | 0 | 1.50 | 491 |
| schema | 0 | 1.75 | 713 |
| doctor | 0 | 2.73 | 10130 |
| mcp:project_query | 0 | 1.87 | 5627 |
| mcp:analyze | 0 | 4.76 | 9159 |
| update | 0 | 24.79 | 1232 |
| review | 3 | 6.33 | 76798 |
| update:revert | 0 | 21.29 | 1232 |

## guzzle/guzzle 8.2.0

Verinoda 0.3.2 at a96f60e, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T02:39:09.

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
| init | 0 | 1.29 | 48 |
| scan | 0 | 23.43 | 1191 |
| gold:def-transfer | 0 | 1.39 | 1046 |
| gold:sendasync-calls-transfer | 0 | 1.47 | 1553 |
| gold:send-calls-sendasync | 0 | 1.43 | 1527 |
| gold:requestasync-calls-transfer | 0 | 1.36 | 1571 |
| gold:send-reaches-apply-options | 0 | 1.40 | 1031 |
| gold:stack-create-chooses-handler | 1 | 1.35 | 787 |
| gold:stack-create-pushes-redirect | 1 | 1.44 | 782 |
| gold:def-choose-handler | 0 | 1.35 | 1027 |
| gold:query-redirects | 0 | 1.58 | 5685 |
| gold:query-cookies | 0 | 1.53 | 5694 |
| query1 | 0 | 1.77 | 5741 |
| query2 | 0 | 1.57 | 5680 |
| query3 | 0 | 1.56 | 5735 |
| analyze1 | 0 | 5.08 | 13012 |
| analyze2 | 0 | 4.32 | 9282 |
| trace | 0 found | 1.42 | 850 |
| q1 | 0 | 1.34 | 1282 |
| q2 | 0 | 1.40 | 2010 |
| map:dependencies | 0 | 1.54 | 9592 |
| map:dataflow | 0 | 2.42 | 1234 |
| map:dead | 0 | 3.00 | 100702 |
| map:hotspots | 0 | 1.47 | 1299 |
| routes | 0 | 1.22 | 491 |
| schema | 0 | 1.37 | 713 |
| doctor | 0 | 2.74 | 10154 |
| mcp:project_query | 0 | 1.67 | 6171 |
| mcp:analyze | 0 | 4.98 | 10007 |
| update | 0 | 13.11 | 1462 |
| review | 3 | 4.14 | 12597 |
| update:revert | 0 | 10.78 | 1209 |
