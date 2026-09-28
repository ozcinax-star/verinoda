# D64 - Ranked unknowns of the name check (pending merge into DESIGN.md / UPGRADING.md)

## DESIGN section

## NN. Ranked unknowns, declared types (D64, 2026-09-28)

### NN.1 Why

A study of the name check's `unknown` verdicts (the diff of the last 20 commits of this repository, 568 sites)
found 125 correct sites and 37 planted misspellings in one undifferentiated `unknown` list, sorted by path. MCP
`code_check` cuts its answer at 12,000 characters, and a listed site averaged 610 characters: the first answer
kept the 12-13 absent sites and none of the 37 planted unknowns. Most unknowns were correct code whose receiver
type jedi did not infer; two jedi 0.20 defects accounted for many of them (a comprehension variable used in the
comprehension's `if` clause infers to nothing; `Path / "x"` infers to `PurePath`, so `mkdir`, `write_text` and
`exists` are not found), and pytest's own fixtures (`tmp_path`, `monkeypatch`) had no type at all. Two kinds of
false `absent` were also seen: an import inside `with raises(ImportError)` that the test expects to fail, and an
optional dependency listed only in a requirements file under `tests/`.

### NN.2 Decisions

- **A rank for every unknown Python site** (`rank`, `rank_why`, `codecheck_rank.py`); the verdict never changes.
  - HIGH: the name is defined nowhere in a word index of the project, its environment's Python sources and
    jedi's stubs; or the receiver's declared type lacks it and has a close name (edit similarity >= 0.8).
  - MEDIUM: the declared type lacks it (only a subclass or runtime code could add it); a `**kwargs` callee would
    take a name defined nowhere; the receiver is bound by an import that is not installed; an import that was
    not decided; a name not found in an index that stopped early.
  - LOW: the receiver's type is not known and the name is defined somewhere.
- **The word index is a superset test.** Every identifier-like word in the text of site-packages, the standard
  library and jedi's typeshed counts as defined (comments and docstrings included), plus option strings as
  argparse turns them into names (`"--no-mcp"` -> `no_mcp`) and the running interpreter's built-in names. The
  project's other files count with their words; the checked files only with the names they define (definitions,
  parameters, stores, imports, string constants), so the misspelling itself never counts. Names that only a
  compiled extension defines are outside the index; a `__getattr__` on the receiver's container keeps a site out
  of HIGH. The environment's index is built once per environment fingerprint (and jedi version), kept in memory
  and, with `.verinoda/`, in `.verinoda/cache/check/names-<fingerprint>.json`; the build stops after 120 s
  (`VERINODA_NAME_INDEX_BUDGET_S`), and a name not found in an incomplete index is MEDIUM, never HIGH.
- **Order and listing.** Sites are listed absent, HIGH, not installed, MEDIUM, guarded, then LOW. LOW sites are
  counted by cause in `unknown_summary` (`high`, `medium`, `low`, `low_by_cause` with an example and one next
  step per cause) and listed only with `--all` / `include_exists`. The ranking runs after the per-file cache, since
  a rank depends on what every other file defines. Java, Kotlin and TypeScript sites are not ranked and stay
  listed. Every unknown carries a one-step `next_step` (the cause's step when the site had none).
- **MCP one-line sites.** `code_check` returns each site as one line: `VERDICT path:line:col kind expr | why |
  nearest: names` (or `next: step` when there are no nearest names); HIGH, MEDIUM and LOW label unknowns; the
  `files` list keeps only files that could not be read. The cap cuts from the end, so an absent or HIGH site is
  never cut before a LOW one.
- **Declared types decide `exists`, never `absent` (the D32 asymmetry).** When jedi's goto finds nothing, the
  receiver's declared type is read: jedi's inference of the receiver; a comprehension variable at its `for`
  target; a local bound once to a path join, and `a / b` itself, from the left operand's pathlib class; a call
  from the called function's declared return type (jedi `execute`); an unannotated parameter of a `test*`
  function or a fixture in a pytest file, named like one of pytest's own fixtures, from that fixture's class
  (unless the project defines a fixture of the same name). A name in that type is `exists`. A name it lacks stays
  `unknown` and carries `declared` and `nearest` only when the class is closed in itself (then only a subclass
  can add it); a keyword outside the declared method's signature is `unknown` with the same fields.
- **False absents.** A site inside `with raises(E)` (`pytest.raises`, `assertRaises`, sympy's `raises`) whose E
  is the error that kind of site raises (an import: also a broad `Exception`) is `guarded`. A module whose package
  a requirements file anywhere in the project lists (`tests/requirements/postgres.txt`, a bare name counts) is
  `not_installed` with `optional: true`, not `absent`.
- Cached answers of the previous rule set are not reused (`CHECK_VERSION` 5).

### NN.3 Measured

`verinoda check --json --all --no-cache --diff HEAD~20` on two clones of this repository at dd60358 with 50 names
planted on changed lines (30 attributes, 20 keywords), the worktree's `.venv` as the environment; 568 sites
each. The planting and triage rules were written by the rule author (in-sample).

| | planted: absent / HIGH / MEDIUM / LOW | real code: absent / HIGH / MEDIUM / LOW | real unknowns | planted in the first MCP answer |
|---|---|---|---|---|
| misspellings (two middle letters swapped), before | 13 / - / 37 unranked | 0 / - / 125 unranked | 125 | 12 of 50 |
| misspellings, after | 13 / 30 / 7 / 0 | 0 / 0 / 4 / 66 | 70 | 42 of 50 |
| invented names (`charset=`, `make_dir`, `tokenize`), before | 14 / - / 36 unranked | 0 / - / 131 unranked | 131 | 12 of 50 |
| invented names, after | 14 / 12 / 10 / 14 | 0 / 0 / 3 / 64 | 67 | 36 of 50 |

- `exists` on the same 568 sites: 393 -> 448 (misspelling clone), 387 -> 451 (invented clone), from declared types.
- The 7 planted MEDIUM misspellings are `Field(descirption=)` (pydantic's `Field` takes `**extra`) and two
  `rpeo` on declared types whose close names were not close enough; the 14 LOW invented names are names defined
  elsewhere on receivers whose type is still not known (`m.group(2).trim`, `Field(descriptions=)`,
  `info.update(commits=)`, which is valid code).
- Time on a loaded machine (four builders): 105 s -> 130 s and 79 s -> 123 s for the whole check; the word index
  of the environment (16,660 files) took 20-41 s of that and is paid once per environment when the project has
  `.verinoda/` (the clones had none, so every run built it). jedi time fell from 47-55 s to 19-25 s.
- False absents: on a copy of a Django checkout with its `.venv`, `django/contrib/postgres/signals.py` has 1
  `not_installed (optional)` import (psycopg, listed in `tests/requirements/postgres.txt`) instead of an absent;
  the two `psycopg2` imports stay absent (psycopg2 is listed in no requirements file; they sit under
  `if is_psycopg3:`, a flag imported from a module that sets it in try/except ImportError, which is not read as a
  guard).
- Tests: `tests/test_codecheck_rank.py` (ranks, summary, order, the name index's superset rule, argparse dests,
  not-installed receivers, raises guards, optional dependencies, declared types for the two jedi defects, path-join
  locals, call results and pytest fixtures, MCP one-line sites under the cap, CLI labels).

### NN.4 Not done

- `**kwargs` following (the study's M2), mypy or pyright as a second resolver, the opt-in runtime probes.
- A flag imported from another module (`is_psycopg3`) as an import guard.
- A user-level cache of the environment's word index for projects without `.verinoda/`.

## Decision table row

| D64 | Ranked unknowns, declared types | implemented | Built 2026-09-28 (section NN): unknown Python sites are ranked HIGH (a name defined nowhere in a word index of the project, its environment and the stubs, or a close misspelling of the receiver's declared type), MEDIUM or LOW; LOW sites are counted by cause in `unknown_summary` and listed only with `--all`; sites are listed absent, HIGH, not installed, MEDIUM, guarded, LOW, and MCP `code_check` gives one line per site; declared types (jedi's inference, comprehension `for` targets, pathlib joins, declared return types, pytest's own fixtures) decide `exists`, never `absent`; `with raises(E)` is a guard and a module listed in any requirements file is `not_installed` (optional). On a planted-misspelling diff: 50 planted sites all absent/HIGH/MEDIUM, 0 real HIGH, real unknowns 125 -> 70, planted sites in the first MCP answer 12 -> 42. Not done: `**kwargs` following, mypy/pyright, runtime probes. |

## UPGRADING note

- D64: `verinoda check --json` and MCP `code_check` list fewer unknown sites by default. Each unknown Python site
  has `rank` (`high` / `medium` / `low`) and `rank_why`; LOW sites are no longer in `sites` unless `--all`
  (`include_exists=true`) is given - `summary.unknown` still counts them, and the new `unknown_summary` gives the
  counts per rank and the LOW sites per cause. Sites are ordered absent, HIGH unknown, not installed, MEDIUM
  unknown, guarded, LOW unknown (then path and line), no longer by verdict alone. A script that read every unknown
  from `sites` should pass `--all`.
- MCP `code_check`: `sites` is a list of one-line strings (`VERDICT path:line:col kind expr | ...`), no longer of
  objects, and `files` lists only the files that could not be read (`summary.files` counts all). The CLI's `--json`
  keeps objects.
- Text output labels unknowns `unknown HIGH` / `unknown MEDIUM` / `unknown LOW` and ends with a line of rank
  counts and the largest LOW causes.
- More sites are `exists` than before (a receiver's declared type is read where jedi's goto found nothing), an
  import inside `with pytest.raises(ImportError)` is `guarded`, and a module listed in a requirements file anywhere
  in the project is `not_installed` with `optional: true` instead of `absent`.
- Cached check answers are recomputed once (`CHECK_VERSION` 5). The first check in an environment builds a word
  index of its sources (about 15-40 s for 16,000 files) and keeps it in `.verinoda/cache/check/names-*.json`
  (derived, safe to delete); `VERINODA_NAME_INDEX_BUDGET_S` (default 120) bounds that build.
- `docs/ARCHITECTURE.md` gained one row for `codecheck_rank.py` (tests/test_docs.py requires every module there).
