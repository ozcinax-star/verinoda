# Real-world run 2026-10-02

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs.

Times in seconds, wall clock, one process at a time. Gold v1: the frozen facts; v2: the corrected checks added after review.

| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | clean after | gold v1 | gold v2 |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| fastapi/full-stack-fastapi-template (Python + TypeScript) | 254 | 17.8 | 11.7 | 1.4 / 3.4 | 0 | 0 | yes | 10/10 | - |
| fastapi/sqlmodel (Python) | 526 | 27.7 | 17.6 | 1.5 / 2.7 | 0 | 0 | yes | 10/10 | - |
| expressjs/express (JavaScript) | 218 | 10.9 | 8.8 | 1.3 / 2.7 | 0 | 0 | yes | 7/10 | 2/2 |
| axios/axios (JavaScript + TypeScript) | 466 | 25.6 | 19.4 | 1.5 / 3.6 | 0 | 0 | yes | 9/10 | - |
| junegunn/fzf (Go) | 161 | 15.5 | 8.6 | 1.6 / 3.4 | 0 | 0 | yes | 8/10 | 3/3 |
| gin-gonic/gin (Go) | 130 | 11.9 | 8.0 | 1.4 / 2.6 | 0 | 0 | yes | 8/10 | 2/2 |
| sharkdp/bat (Rust) | 974 | 34.7 | 19.2 | 1.5 / 3.1 | 0 | 0 | yes | 10/10 | - |
| google/gson (Java) | 311 | 27.2 | 24.9 | 1.9 / 4.0 | 0 | 0 | yes | 10/10 | - |
| guzzle/guzzle (PHP) | 176 | 23.1 | 13.1 | 1.5 / 4.6 | 0 | 0 | yes | 10/10 | - |

## fastapi/full-stack-fastapi-template 0.12.0

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:39:45.

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
| init | 0 | 2.87 | 70 |
| scan | 0 | 17.79 | 1324 |
| gold:def-authenticate | 0 | 1.29 | 1051 |
| gold:login-calls-authenticate | 0 | 1.23 | 1631 |
| gold:login-reaches-verify-password | 0 | 1.38 | 1023 |
| gold:users-create-calls-crud | 0 | 1.26 | 1669 |
| gold:def-get-current-user | 0 | 1.22 | 1084 |
| gold:route-login-access-token | 0 | 1.22 | 8420 |
| gold:route-read-item | 0 | 1.20 | 8420 |
| gold:schema-user-table | 0 | 1.33 | 9333 |
| gold:frontend-login-calls-sdk | 0 | 1.21 | 1720 |
| gold:query-jwt | 0 | 1.39 | 5666 |
| query1 | 0 | 1.42 | 5734 |
| query2 | 0 | 1.44 | 5716 |
| query3 | 0 | 1.38 | 5678 |
| analyze1 | 0 | 3.02 | 10515 |
| analyze2 | 0 | 3.79 | 12760 |
| trace | 0 found | 1.23 | 1023 |
| q1 | 0 | 1.21 | 1210 |
| q2 | 0 | 1.21 | 4495 |
| check | 0 checked | 3.36 | 2918 |
| map:dependencies | 0 | 1.23 | 9593 |
| map:dataflow | 0 | 1.99 | 10018 |
| map:dead | 0 | 2.58 | 94866 |
| map:hotspots | 0 | 1.46 | 1299 |
| routes | 0 | 1.19 | 8420 |
| schema | 0 | 1.34 | 9333 |
| taint | 3 | 1.62 | 3543 |
| doctor | 0 | 2.70 | 10831 |
| mcp:project_query | 0 | 1.46 | 6250 |
| mcp:analyze | 0 | 2.60 | 7570 |
| update | 0 | 11.67 | 1322 |
| review | 0 | 3.65 | 11211 |
| update:revert | 0 | 10.63 | 1322 |

## fastapi/sqlmodel 0.0.47

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:41:21.

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
| init | 0 | 1.30 | 51 |
| scan | 0 | 27.73 | 1295 |
| gold:def-get-column-from-field | 0 | 1.33 | 1079 |
| gold:metaclass-new-calls-column | 0 | 1.36 | 1619 |
| gold:column-calls-satype | 0 | 1.36 | 727 |
| gold:model-validate-calls-compat | 0 | 1.37 | 1638 |
| gold:model-validate-reaches-compat | 0 | 1.35 | 729 |
| gold:def-field-impl | 0 | 1.34 | 1015 |
| gold:class-sqlmodel | 0 | 1.32 | 1013 |
| gold:schema-hero-table | 0 | 1.72 | 81381 |
| gold:query-field-to-column | 0 | 1.57 | 5739 |
| gold:query-session-exec | 0 | 1.49 | 5765 |
| query1 | 0 | 1.58 | 5695 |
| query2 | 0 | 1.53 | 5676 |
| query3 | 0 | 1.48 | 5712 |
| analyze1 | 0 | 2.82 | 9942 |
| analyze2 | 0 | 2.66 | 9290 |
| trace | 0 found | 1.35 | 729 |
| q1 | 0 | 1.33 | 1127 |
| q2 | 0 | 1.36 | 1422 |
| check | 0 checked | 5.14 | 18967 |
| map:dependencies | 0 | 1.27 | 10188 |
| map:dataflow | 0 | 2.25 | 105113 |
| map:dead | 0 | 3.48 | 117038 |
| map:hotspots | 0 | 1.46 | 1299 |
| routes | 0 | 1.48 | 51570 |
| schema | 0 | 1.71 | 81381 |
| taint | 0 | 2.29 | 1290 |
| doctor | 0 | 2.77 | 10343 |
| mcp:project_query | 0 | 1.46 | 5434 |
| mcp:analyze | 0 | 2.96 | 7540 |
| update | 0 | 17.61 | 1309 |
| review | 0 | 3.22 | 11185 |
| update:revert | 0 | 12.85 | 1306 |

## expressjs/express v5.2.1

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:43:19.

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
| init | 0 | 1.32 | 52 |
| scan | 0 | 10.88 | 1209 |
| gold:def-stringify | 0 | 1.17 | 1028 |
| gold:json-calls-stringify | 0 | 1.18 | 1501 |
| gold:json-calls-send | 0 | 1.22 | 1498 |
| gold:sendfile-calls-helper | 0 | 1.25 | 781 |
| gold:render-reaches-tryrender | 2 | 1.25 | 424 |
| gold:def-create-application | 0 | 1.18 | 1020 |
| gold:route-posts | 0 | 1.49 | 73704 |
| gold:route-user-edit-put | 0 | 1.51 | 73704 |
| gold:query-json-response | 0 | 1.38 | 4281 |
| gold:query-accepts | 0 | 1.43 | 3795 |
| gold:route-posts@v2 | 0 | 1.44 | 73704 |
| gold:route-user-edit-put@v2 | 0 | 1.48 | 73704 |
| query1 | 0 | 1.37 | 5624 |
| query2 | 0 | 1.35 | 3957 |
| query3 | 0 | 1.33 | 4470 |
| analyze1 | 0 | 2.79 | 12401 |
| analyze2 | 0 | 2.56 | 10364 |
| trace | 0 found | 1.25 | 588 |
| q1 | 0 | 1.18 | 1145 |
| q2 | 0 | 1.19 | 2541 |
| map:dependencies | 0 | 1.16 | 8129 |
| map:dataflow | 0 | 1.46 | 4733 |
| map:dead | 0 | 1.91 | 77750 |
| map:hotspots | 0 | 1.37 | 1299 |
| routes | 0 | 1.45 | 73704 |
| schema | 0 | 1.26 | 713 |
| doctor | 0 | 2.54 | 10334 |
| mcp:project_query | 0 | 1.25 | 5729 |
| mcp:analyze | 0 | 2.88 | 8911 |
| update | 0 | 8.82 | 2428 |
| review | 0 | 2.98 | 11174 |
| update:revert | 0 | 8.84 | 1225 |

## axios/axios v1.20.0

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:44:36.

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
| init | 0 | 1.30 | 46 |
| scan | 0 | 25.56 | 1185 |
| gold:def-dispatch-request | 0 | 4.02 | 1052 |
| gold:request-calls-private | 0 | 1.36 | 1575 |
| gold:private-request-merges-config | 0 | 1.36 | 1556 |
| gold:dispatch-checks-cancel | 0 | 1.34 | 841 |
| gold:request-reaches-adapter | 2 | 1.37 | 591 |
| gold:create-instance-binds | 0 | 1.32 | 1463 |
| gold:def-merge-config | 0 | 1.30 | 1065 |
| gold:def-get-adapter | 0 | 1.31 | 1065 |
| gold:query-interceptors | 0 | 1.57 | 5398 |
| gold:query-http-adapter | 0 | 1.58 | 4594 |
| query1 | 0 | 1.51 | 5596 |
| query2 | 0 | 1.55 | 5691 |
| query3 | 0 | 1.45 | 5382 |
| analyze1 | 0 | 4.39 | 9623 |
| analyze2 | 0 | 2.90 | 9051 |
| trace | 2 no directed path | 1.37 | 518 |
| q1 | 0 | 1.30 | 1141 |
| q2 | 0 | 1.33 | 3998 |
| map:dependencies | 0 | 1.26 | 9400 |
| map:dataflow | 0 | 2.54 | 2682 |
| map:dead | 0 | 3.31 | 104280 |
| map:hotspots | 0 | 1.42 | 1299 |
| routes | 0 | 1.56 | 36434 |
| schema | 0 | 1.38 | 713 |
| doctor | 0 | 2.69 | 10543 |
| mcp:project_query | 0 | 1.45 | 5447 |
| mcp:analyze | 0 | 4.51 | 7958 |
| update | 0 | 19.40 | 1568 |
| review | 0 | 3.71 | 11957 |
| update:revert | 0 | 17.31 | 1216 |

## junegunn/fzf v0.74.4

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:46:37.

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
| scan | 0 | 15.49 | 1186 |
| gold:def-run | 0 | 1.25 | 985 |
| gold:main-calls-run | 2 | 1.27 | 313 |
| gold:parse-calls-parse | 0 | 1.26 | 1514 |
| gold:run-calls-postprocess | 0 | 1.26 | 1582 |
| gold:run-calls-newmatcher | 0 | 1.39 | 615 |
| gold:matcher-loop-scan | 0 | 1.27 | 1471 |
| gold:def-fuzzymatchv2 | 0 | 1.25 | 1009 |
| gold:pattern-matchitem | 0 | 1.28 | 1539 |
| gold:query-fuzzy-algo | 0 | 1.51 | 5677 |
| gold:query-options | 0 | 1.54 | 5235 |
| gold:main-calls-run@v2 | 0 | 1.33 | 484 |
| gold:run-calls-newmatcher@v2 | 0 | 1.27 | 542 |
| gold:pattern-matchitem@v2 | 0 | 1.27 | 1539 |
| query1 | 0 | 1.60 | 5672 |
| query2 | 0 | 1.64 | 5703 |
| query3 | 0 | 1.41 | 5633 |
| analyze1 | 0 | 4.30 | 9921 |
| analyze2 | 0 | 2.51 | 9522 |
| trace | 0 found | 1.26 | 683 |
| q1 | 0 | 1.23 | 1176 |
| q2 | 0 | 1.26 | 1947 |
| map:dependencies | 0 | 1.24 | 7794 |
| map:dataflow | 0 | 2.43 | 1509 |
| map:dead | 0 | 2.71 | 136603 |
| map:hotspots | 0 | 1.38 | 1299 |
| routes | 0 | 1.15 | 491 |
| schema | 0 | 1.27 | 713 |
| doctor | 0 | 2.46 | 10348 |
| mcp:project_query | 0 | 1.54 | 6321 |
| mcp:analyze | 0 | 4.33 | 8285 |
| update | 0 | 8.61 | 1598 |
| review | 3 | 3.02 | 14150 |
| update:revert | 0 | 7.03 | 1201 |

## gin-gonic/gin v1.12.0

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:48:03.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-default` (q)
- hit  `default-calls-new` (q)
- MISS `default-calls-recovery` (trace): source not resolved (ambiguous)
- hit  `servehttp-calls-handle` (q)
- MISS `request-reaches-tree` (trace): no path edge at tree.go
- hit  `get-registers-route` (q)
- hit  `recovery-calls-writer` (q)
- hit  `context-json-method` (q)
- hit  `query-radix-tree` (query): rank 1
- hit  `query-recovery` (query): rank 2
- hit  `default-calls-recovery@v2` v2 (trace)
- hit  `request-reaches-tree@v2` v2 (trace)

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.28 | 48 |
| scan | 0 | 11.88 | 1191 |
| gold:def-default | 0 | 1.26 | 983 |
| gold:default-calls-new | 0 | 1.24 | 1363 |
| gold:default-calls-recovery | 2 | 1.30 | 935 |
| gold:servehttp-calls-handle | 0 | 1.24 | 1569 |
| gold:request-reaches-tree | 0 | 1.32 | 946 |
| gold:get-registers-route | 0 | 1.25 | 2073 |
| gold:recovery-calls-writer | 0 | 1.25 | 1475 |
| gold:context-json-method | 0 | 1.24 | 1023 |
| gold:query-radix-tree | 0 | 1.49 | 5715 |
| gold:query-recovery | 0 | 1.37 | 5201 |
| gold:default-calls-recovery@v2 | 0 | 1.28 | 513 |
| gold:request-reaches-tree@v2 | 0 | 1.30 | 946 |
| query1 | 0 | 1.40 | 5690 |
| query2 | 0 | 1.51 | 5646 |
| query3 | 0 | 1.44 | 5652 |
| analyze1 | 0 | 2.71 | 11657 |
| analyze2 | 0 | 2.56 | 9091 |
| trace | 0 found | 1.29 | 719 |
| q1 | 0 | 1.26 | 1208 |
| q2 | 0 | 1.27 | 2857 |
| map:dependencies | 0 | 1.21 | 8479 |
| map:dataflow | 0 | 1.75 | 3292 |
| map:dead | 0 | 2.19 | 136474 |
| map:hotspots | 0 | 1.42 | 1299 |
| routes | 0 | 1.19 | 491 |
| schema | 0 | 1.31 | 713 |
| doctor | 0 | 2.42 | 10123 |
| mcp:project_query | 0 | 1.32 | 6216 |
| mcp:analyze | 0 | 2.74 | 8751 |
| update | 0 | 8.03 | 1199 |
| review | 3 | 3.16 | 14691 |
| update:revert | 0 | 6.61 | 1199 |

## sharkdp/bat v0.26.1

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:49:19.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-run-controller` (q)
- hit  `run-controller-new` (q)
- hit  `controller-run-delegates` (q)
- hit  `main-reaches-run-controller` (trace)
- hit  `run-reaches-print-file-ranges` (trace)
- hit  `def-interactive-print-line` (q)
- hit  `struct-controller` (q)
- hit  `print-file-calls-header` (q)
- hit  `query-pager` (query): rank 4
- hit  `query-highlight` (query): rank 2

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.27 | 46 |
| scan | 0 | 34.73 | 2182 |
| gold:def-run-controller | 0 | 1.43 | 1026 |
| gold:run-controller-new | 0 | 1.39 | 1566 |
| gold:controller-run-delegates | 0 | 1.37 | 1644 |
| gold:main-reaches-run-controller | 0 | 1.40 | 826 |
| gold:run-reaches-print-file-ranges | 0 | 1.39 | 1442 |
| gold:def-interactive-print-line | 0 | 1.37 | 1691 |
| gold:struct-controller | 0 | 1.34 | 987 |
| gold:print-file-calls-header | 0 | 1.38 | 1703 |
| gold:query-pager | 0 | 1.64 | 5588 |
| gold:query-highlight | 0 | 1.62 | 5637 |
| query1 | 0 | 1.60 | 5631 |
| query2 | 0 | 1.53 | 5592 |
| query3 | 0 | 1.55 | 5659 |
| analyze1 | 0 | 3.08 | 10750 |
| analyze2 | 0 | 3.15 | 11598 |
| trace | 0 found | 1.41 | 995 |
| q1 | 0 | 1.37 | 1240 |
| q2 | 0 | 1.44 | 1533 |
| map:dependencies | 0 | 1.32 | 7911 |
| map:dataflow | 0 | 1.98 | 3996 |
| map:dead | 0 | 4.60 | 72167 |
| map:hotspots | 0 | 1.56 | 1299 |
| routes | 0 | 1.39 | 491 |
| schema | 0 | 1.48 | 713 |
| doctor | 0 | 2.86 | 14099 |
| mcp:project_query | 0 | 1.50 | 6216 |
| mcp:analyze | 0 | 3.39 | 8327 |
| update | 0 | 19.25 | 2202 |
| review | 3 | 4.30 | 12775 |
| update:revert | 0 | 16.31 | 2203 |

## google/gson gson-parent-2.14.0

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:51:25.

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
| init | 0 | 1.28 | 46 |
| scan | 0 | 27.16 | 1185 |
| gold:def-dopeek | 0 | 1.60 | 1212 |
| gold:peek-calls-dopeek | 0 | 1.62 | 1951 |
| gold:tojson-calls-getadapter | 0 | 1.62 | 3037 |
| gold:getadapter-calls-factory-create | 0 | 1.63 | 2009 |
| gold:fromjson-calls-getadapter | 0 | 1.61 | 3065 |
| gold:parsestring-reaches-streams | 0 | 1.69 | 2327 |
| gold:boundfields-calls-create | 0 | 1.66 | 2293 |
| gold:class-reflective-factory | 0 | 1.67 | 1254 |
| gold:query-reflective | 0 | 1.98 | 5666 |
| gold:query-json-reader | 0 | 1.93 | 5705 |
| query1 | 0 | 1.97 | 5745 |
| query2 | 0 | 1.94 | 5740 |
| query3 | 0 | 1.88 | 5737 |
| analyze1 | 0 | 4.55 | 11282 |
| analyze2 | 0 | 3.41 | 8911 |
| trace | 0 found | 1.68 | 1033 |
| q1 | 0 | 1.62 | 1351 |
| q2 | 0 | 1.68 | 22683 |
| check | 0 checked | 1.68 | 35611 |
| map:dependencies | 0 | 1.63 | 12846 |
| map:dataflow | 0 | 2.80 | 2545 |
| map:dead | 0 | 4.02 | 162293 |
| map:hotspots | 0 | 1.75 | 1299 |
| routes | 0 | 1.51 | 491 |
| schema | 0 | 1.65 | 713 |
| doctor | 0 | 2.75 | 10130 |
| mcp:project_query | 0 | 1.85 | 5627 |
| mcp:analyze | 0 | 4.60 | 9159 |
| update | 0 | 24.87 | 1232 |
| review | 3 | 6.63 | 76798 |
| update:revert | 0 | 20.56 | 1231 |

## guzzle/guzzle 8.2.0

Verinoda 0.3.2 at 7e61ce8, Python 3.13.14, Windows-11-10.0.26200-SP0, 16 CPUs; started 2026-10-02T04:53:46.

Skipped: check: no Python, Java or Kotlin file (check reads only those)

Gold facts:

- hit  `def-transfer` (q)
- hit  `sendasync-calls-transfer` (q)
- hit  `send-calls-sendasync` (q)
- hit  `requestasync-calls-transfer` (q)
- hit  `send-reaches-apply-options` (trace)
- hit  `stack-create-chooses-handler` (q)
- hit  `stack-create-pushes-redirect` (q)
- hit  `def-choose-handler` (q)
- hit  `query-redirects` (query): rank 1
- hit  `query-cookies` (query): rank 3

| step | exit | s | stdout bytes |
|---|---:|---:|---:|
| init | 0 | 1.28 | 48 |
| scan | 0 | 23.10 | 1193 |
| gold:def-transfer | 0 | 1.38 | 1047 |
| gold:sendasync-calls-transfer | 0 | 1.43 | 1553 |
| gold:send-calls-sendasync | 0 | 1.37 | 1527 |
| gold:requestasync-calls-transfer | 0 | 1.36 | 1571 |
| gold:send-reaches-apply-options | 0 | 1.40 | 1031 |
| gold:stack-create-chooses-handler | 0 | 1.36 | 1600 |
| gold:stack-create-pushes-redirect | 0 | 1.35 | 1602 |
| gold:def-choose-handler | 0 | 1.35 | 1027 |
| gold:query-redirects | 0 | 1.59 | 5711 |
| gold:query-cookies | 0 | 1.62 | 5674 |
| query1 | 0 | 1.82 | 5694 |
| query2 | 0 | 1.53 | 5688 |
| query3 | 0 | 1.52 | 5705 |
| analyze1 | 0 | 5.17 | 13936 |
| analyze2 | 0 | 4.11 | 9296 |
| trace | 0 found | 1.45 | 850 |
| q1 | 0 | 1.49 | 1266 |
| q2 | 0 | 1.39 | 2011 |
| map:dependencies | 0 | 1.29 | 9569 |
| map:dataflow | 0 | 2.37 | 1234 |
| map:dead | 0 | 2.71 | 96500 |
| map:hotspots | 0 | 1.51 | 1299 |
| routes | 0 | 1.24 | 491 |
| schema | 0 | 1.37 | 713 |
| doctor | 0 | 2.55 | 10154 |
| mcp:project_query | 0 | 1.66 | 6147 |
| mcp:analyze | 0 | 5.14 | 10559 |
| update | 0 | 13.06 | 1462 |
| review | 3 | 3.90 | 12597 |
| update:revert | 0 | 13.19 | 1209 |
