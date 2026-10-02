# Real-world run 2, 2026-10-02 (Verinoda a96f60e)

The second full run, after the defect fixes D165-D168 (definition lines, route prefixes, distinct ids, JavaScript
assigned methods). Same pinned repositories, same frozen gold. Nine repositories in `summary.md`; Graphify ran from its
own clone (`--work C:/vbench-rw`) and is in `graphify/`.

| | run 1 (f4ca326) | run 2 (a96f60e) |
|---|---:|---:|
| gold v1 | 77/100 | 87/100 |
| gold v2 | 6/7 | 6/7 |
| crashes / timeouts | 0 / 0 | 0 / 0 |

Per repository, gold v1, run 1 -> run 2: full-stack-fastapi-template 7 -> 10, sqlmodel 9 -> 10, express 4 -> 7,
axios 7 -> 9, fzf 8 -> 8, gin 7 -> 7, bat 8 -> 8, gson 9 -> 10, guzzle 8 -> 8, graphify 10 -> 10.
Still open: defects 5 and 6 of `../realworld-2026-10-02/defects.md` (PHP static calls, Rust `Type::new`, method calls
on locals whose type is evident).
