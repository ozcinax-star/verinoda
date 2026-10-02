# tq audit, 2026-10-02

Commit 3bfe97c04b80; gold sha256 4962f768961f (tq_gold/held_out.json, tq_gold2/held_out.json); engine sha256 ebc2265cc8c8.

Held-out: 357 cases, 0 skipped, 350 decided (347 right, 3 wrong), 7 unknown; wrong at a verified status: 2. Dev: 75 cases.

Cells: 22; 4 with n >= 30 (4 of them decided); 3 shown by tq as `measured`.

## Cells (held-out)

| type | answer | status | n | right | wrong | Wilson 95% | dev right/n | shown |
|---|---|---|---|---|---|---|---|---|
| callers | count | strong_inference | 2 | 2 | 0 | 0.342-1.000 | 8/8 | no: n < 30 |
| calls | ? | unknown | 4 | 0 | 0 | 0.000-0.490 | 0/0 | no: not a decided answer |
| calls | no | strong_inference | 64 | 64 | 0 | 0.943-1.000 | 7/7 | yes |
| calls | no | weak_inference | 25 | 24 | 1 | 0.805-0.993 | 0/0 | no: n < 30 |
| calls | yes | statically_verified | 77 | 75 | 2 | 0.910-0.993 | 10/10 | no: dev precision outside the held-out interval |
| calls | yes | strong_inference | 6 | 6 | 0 | 0.610-1.000 | 0/0 | no: n < 30 |
| exists | no | strong_inference | 82 | 82 | 0 | 0.955-1.000 | 3/3 | yes |
| exists | yes | statically_verified | 77 | 77 | 0 | 0.953-1.000 | 7/7 | yes |
| exists | yes | strong_inference | 1 | 1 | 0 | 0.206-1.000 | 0/0 | no: n < 30 |
| q | yes | statically_verified | 1 | 1 | 0 | 0.206-1.000 | 0/0 | no: n < 30 |
| reaches | no | weak_inference | 3 | 3 | 0 | 0.439-1.000 | 4/5 | no: n < 30 |
| reaches | yes | weak_inference | 1 | 1 | 0 | 0.206-1.000 | 2/2 | no: n < 30 |
| reads | yes | weak_inference | 3 | 3 | 0 | 0.439-1.000 | 2/2 | no: n < 30 |
| route | ? | unknown | 1 | 0 | 0 | 0.000-0.793 | 0/1 | no: not a decided answer |
| route | no | weak_inference | 1 | 1 | 0 | 0.206-1.000 | 0/0 | no: n < 30 |
| route | yes | statically_verified | 1 | 1 | 0 | 0.206-1.000 | 2/2 | no: n < 30 |
| route | yes | weak_inference | 1 | 1 | 0 | 0.206-1.000 | 0/0 | no: n < 30 |
| taint | no | weak_inference | 1 | 1 | 0 | 0.206-1.000 | 1/1 | no: n < 30 |
| taint | yes | strong_inference | 1 | 1 | 0 | 0.206-1.000 | 0/0 | no: n < 30 |
| tested | ? | unknown | 2 | 0 | 0 | 0.000-0.658 | 0/2 | no: not a decided answer |
| which | none | strong_inference | 1 | 1 | 0 | 0.206-1.000 | 1/1 | no: n < 30 |
| writes | no | weak_inference | 2 | 2 | 0 | 0.342-1.000 | 2/2 | no: n < 30 |

## Reliability against CONFIDENCE_CAP (held-out, decided answers, all types)

The caps are not changed here; a cap outside the interval is a finding for a reviewed decision.

| status | CONFIDENCE_CAP | n | right | precision | Wilson 95% | cap |
|---|---|---|---|---|---|---|
| experiment_verified | 0.95 | 0 | - | - | - | no answer at this status |
| statically_verified | 0.9 | 156 | 154 | 0.987 | 0.955-0.997 | below |
| primary_source_verified | 0.85 | 0 | - | - | - | no answer at this status |
| observed | 0.9 | 0 | - | - | - | no answer at this status |
| strong_inference | 0.7 | 157 | 157 | 1.000 | 0.976-1.000 | below |
| weak_inference | 0.4 | 37 | 36 | 0.973 | 0.862-0.995 | below |
| stale | 0.3 | 0 | - | - | - | no answer at this status |
| unknown | 0.0 | 0 | - | - | - | no answer at this status |
| contradicted | 0.05 | 0 | - | - | - | no answer at this status |

## Wrong answers

- `v-calls-19` (held_out) `calls bisect _rev_list`: gold true, answered false at weak_inference; why: no calls edge (not read as Python)
- `v-alias-11` (held_out) `calls diff_file is_test_file`: gold false, answered true at statically_verified
- `v-alias-12` (held_out) `calls diff_trees is_test_file`: gold false, answered true at statically_verified
- `q-reaches-10` (dev) `reaches run_by_name target`: gold true, answered false at weak_inference; why: no calls path of 1..6 edges in the static graph (callbacks, getattr, DI not resolved)
