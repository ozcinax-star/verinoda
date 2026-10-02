# Real-world run 3, 2026-10-02 (Verinoda 7e61ce8)

The third full run, after D171 (receivers of a stated type: PHP static calls, Rust `Type::new` and typed locals,
Go receiver chains, JS `call`/`apply`/`bind`) on top of the run-2 fixes. Same pinned repositories, same frozen gold.
Graphify ran from its own clone (`--work C:/vbench-rw`) and is in `graphify/`.

| | run 1 (f4ca326) | run 2 (a96f60e) | run 3 (7e61ce8) |
|---|---:|---:|---:|
| gold v1 | 77/100 | 87/100 | 92/100 |
| gold v2 | 6/7 | 6/7 | 7/7 |
| crashes / timeouts | 0 / 0 | 0 / 0 | 0 / 0 |

Per repository, gold v1, run 1 -> 2 -> 3: full-stack-fastapi-template 7 -> 10 -> 10, sqlmodel 9 -> 10 -> 10,
express 4 -> 7 -> 7, axios 7 -> 9 -> 9, fzf 8 -> 8 -> 8, gin 7 -> 7 -> 8, bat 8 -> 8 -> 10, gson 9 -> 10 -> 10,
guzzle 8 -> 8 -> 10, graphify 10 -> 10 -> 10. The eight misses left are listed per repository in `summary.md`.
