# Never "ok" without looking: measurements, 2026-09-26

What branch `night/no-silent-ok` changed (docs/DESIGN.md D31, D32, D33; docs/BENCHMARKS.md "Update
2026-09-26: never ok without looking") and how it was measured. The repro cases are the senior evaluators'
own (2026-09-25); the code was written after them, so they are in-sample for this change.

Run from the repository root with the project's Python (no network):

    python benchmarks/results/no-silent-ok-2026-09-26/repros.py LABEL

It copies `examples/` into a temporary folder, `git init`s and (for the no_edge case) scans the copy; it
never touches `examples/` itself. Run it with the code of `main` on the path for the "before" file.

## Files

- `repros.py`: the 12 repro cases (only_in over a `com.example` package, no_edge to an external package,
  a Kotlin-DSL build with a version catalog, a pnpm workspace package, a fresh CI clone with and without a
  committed `verinoda.toml`, `check` on a Java file, on `--stdin --as Foo.java` and on a TypeScript rename in
  the diff, invented names inside `except Exception`, a false TypeScript config claim, a Java relation
  grade). `silent` marks the outcome the evaluator reported (a pass, `ok` or a verified status over
  something not checked or false).
- `adr-0002.md`, `snippet2.py`: the evaluators' fixture files (the lead's committed decision record, the
  backend engineer's request handler).
- `repros-main.json`, `repros-branch.json`: the cases on `main` (c8da753) and on the branch.
- `fast-main.json`, `fast-branch.json`: the fast benchmark harness (Verinoda approaches, the eight public
  sets; `fast-main.json` is `int0925c` without the private set).
- `agent-oos.txt`: the agent persona's 10 out-of-sample questions (5 on a copy of Verinoda, 5 on the
  standard library), scored with its own gold patterns (`gold.py`) for query text, query JSON, analyze JSON
  (with a drafted plan) and analyze text, on `main` and on the branch, each on its own copy of the indexes.
