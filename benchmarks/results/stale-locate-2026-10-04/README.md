# What a stale index does to `verinoda locate` (D2, no model run)

Date: 2026-10-04. `benchmarks/agent_compare/stale_locate.py`: for each of the 21 seL4 tasks of `DESIGN_SEL4.md`, the
report's text was given to `verinoda locate` twice: on the task's copy as the assist study built it (the index and the
working tree at the base commit), and on a copy of it whose **working tree was moved 100 commits on while its index was left
as it was** (`git checkout` of the commit 100 steps after the base, on the way to seL4's `master`; the repository, so
the files and their history are the real ones). 120 to 234 files differ between the two trees.

## Result

- **21 tasks; 156 files listed by the stale answers, 0 of them gone from the stale tree.**
  In these 21 tasks none of the files it listed had been deleted since (an index that still knows a file the later commits
  deleted could list it; none did here).
- **The two answers were the same list in 17 of 21 tasks**; the mean overlap (files listed by both over files
  listed by either) is 0.934. 4 tasks' lists moved: i1004, i1015, i1008, i1243.
- **Gold files listed: 18 of 34 fresh, 16 stale.** Two gold files were lost to the staleness (i1015 and i1008, the tasks whose
  lists moved most); none was gained.
- **The stale answer says so**: in all 21 the text carries the note "N file(s) changed since the index (run
  `verinoda update`)". (N is 655 to 1,023, more than the 120 to 234 paths the 100 commits changed; why the check
  counts that many was not looked into here, so the number is a cue to update and not a measure.)

## Not measured

What an agent does with a stale answer (no session was run), a stale index of a repository that has changed more (the tree is 100
commits on), or the cost in tokens: `DESIGN_STALE.md` measured that for `analyze` and found a
stale index cost about a fifth more input tokens than no index on projects over 300 files. This result says only that
`locate` itself degrades gently and tells the agent.

## The tasks

| task | files changed by the 100 commits | listed by both | gold files listed (fresh / stale) |
|---|---|---|---|
| i1566 | 157 | 1.00 | 2 / 2 of 2 |
| i1311 | 157 | 1.00 | 1 / 1 of 1 |
| i749 | 153 | 1.00 | 0 / 0 of 1 |
| i991 | 234 | 1.00 | 1 / 1 of 1 |
| i1004 | 234 | 0.78 | 1 / 1 of 2 |
| i1015 | 120 | 0.46 | 4 / 3 of 6 |
| i1209 | 119 | 1.00 | 1 / 1 of 1 |
| i1104 | 159 | 1.00 | 1 / 1 of 1 |
| i28 | 152 | 1.00 | 1 / 1 of 1 |
| i1199 | 121 | 1.00 | 0 / 0 of 1 |
| i1529 | 128 | 1.00 | 0 / 0 of 1 |
| i1489 | 130 | 1.00 | 1 / 1 of 1 |
| i1170 | 132 | 1.00 | 0 / 0 of 2 |
| i1091 | 139 | 1.00 | 0 / 0 of 3 |
| i805 | 155 | 1.00 | 0 / 0 of 2 |
| i56 | 232 | 1.00 | 1 / 1 of 1 |
| i1008 | 158 | 0.60 | 1 / 0 of 3 |
| i788 | 155 | 1.00 | 1 / 1 of 1 |
| i1243 | 159 | 0.78 | 1 / 1 of 1 |
| i53 | 154 | 1.00 | 1 / 1 of 1 |
| i472 | 234 | 1.00 | 0 / 0 of 1 |
