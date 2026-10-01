# benchmarks/realworld/

Verinoda used the way an end user would, on pinned open-source repositories that it was not built
on. The method and the decisions are in
[docs/drafts/realworld-bench.md](../../docs/drafts/realworld-bench.md).

```
manifest.json        the repositories: name, url, tag, sha, license, stars at selection (with the date),
                     language, and an optional scenario (questions, trace pair, the scripted edit)
gold/<owner>__<name>.json
                     5-10 facts per repository read by hand from the clone at the pinned sha, each with
                     the Verinoda command that checks it; written and committed before Verinoda was run
                     on that repository (corrected checks are appended later as v2 entries)
run.py               the runner (standard library only; Verinoda runs as a subprocess)
mcp_probe.py         one MCP tool (project_query, analyze) called through Verinoda's Python API
report.py            the markdown summary of a results file
```

## How to run

```
python benchmarks/realworld/run.py --repos gin-gonic/gin,junegunn/fzf
python benchmarks/realworld/run.py --smallest 2
python benchmarks/realworld/run.py --all            # every repository, one after the other
python benchmarks/realworld/report.py benchmarks/results/realworld-<date>/results.json
```

Options: `--work DIR` (clone folder, default `C:/vbench`, or `$VERINODA_BENCH_WORK`; keep it outside
OneDrive), `--verinoda-root DIR` (the Verinoda checkout under test, default this one), `--python EXE`,
`--timeout-scale X`, `--out DIR` (default `benchmarks/results/realworld-<date>/`).

Each repository is cloned shallow at its tag, into `<owner>__<name>.partial` first and renamed when
the clone is complete; a `.partial` folder left by a killed run is removed and the clone starts
again. Clones are made with `core.symlinks=false`, `core.longpaths=true` and `GIT_LFS_SKIP_SMUDGE=1`
(no LFS download from another server). An existing clone at the pinned sha is reused; a folder at
another sha is an error and is left alone, and a clone with changes to tracked files is refused
(restore it with `git checkout -- .`). Then: `init` (cold: an untracked `.verinoda/` is removed; a
tracked one is kept and noted), `scan`, the gold checks, 3 `query`, 2 `analyze`, `trace`, 2 `q`,
`check` on one Python, Java or Kotlin file (the languages it reads; skipped and recorded when the
repository has none), `map` views (dependencies, dataflow, dead, hotspots), `routes`, `schema`,
`taint` (Python repositories), `doctor`, the MCP tools `project_query` and `analyze`, then a
scripted one-line edit, `update`, `review`, the edit reverted byte for byte, `update` again, and a
check that `git status` shows no change to a tracked file. Every command is
`python -P -m verinoda ...` (Python 3.11 or later) with `PYTHONPATH` set to the checkout under
test, so a folder of the corpus named like a package is never imported, and with a fresh, empty
`VERINODA_CONFIG_DIR` (no other `VERINODA_*` variable is passed on), so the user's `trust.json` and
`config.json` never apply to a clone.

A step is a crash when its stderr holds a Python traceback, when its exit code is not one the
command documents (the table `OK_CODES` in run.py; gold checks use the codes of their own command:
`query` 0, `trace` 0/2, `q` 0/1/3), or when a non-zero documented exit comes without a JSON object
on stdout (for `q`, one with `rows`): Verinoda's internal errors also exit 1, with `error: ...` on
stderr. The MCP probe exits 1 on an `{"error": ...}` response. A step is a timeout when it runs past
its limit (`TIMEOUTS`, the process tree is killed). The run exits 1 when a repository had a crash, a
timeout, an error, or was not clean after the revert.

Results: `results.json` (per repository: the environment and Verinoda commit, every step's argv,
seconds, exit code, stdout and stderr sizes, whether the JSON parsed, the stderr tail, the answer
status of `trace` and `check`; the gold facts with hit or miss and why; the steps skipped; whether
the clone was clean after the revert) and `summary.md`. Both are written after each repository, and
an unexpected error in one repository is recorded as its `error` while the others still run. A
repository run again on the same day replaces its entry; each entry of `runs` keeps its own
environment. Paths are written as `<CORPUS>`, `<REPO>` and `<HOME>`.

## What is never executed

The repository's code, tests, build and tools. `guard_argv` is an allow list: only `init`, `scan`,
`update`, `query`, `analyze`, `trace`, `q`, `check`, `map`, `routes`, `schema`, `taint`, `review`
and `doctor` may start, each with its own options among `--json`, `--view` (map), `--max-items`
(query) and `--mode` (trace). Any other command (`trust`, `observe`, `debug try`, `run`, `spec`,
`mutations`, ...) and any other token starting with `-` is refused before any process starts,
abbreviations included (`--run-test`, `--obs`: argparse would expand them). The MCP `analyze` is
called with `run_tests=False, observe=False`. `check` uses Verinoda's own interpreter with the
standard library only (no environment of the repository is created). The edit never follows a
symlink or a path that resolves outside the clone. `git` is used for clone, rev-parse, ls-files and
status; Verinoda itself reads the history (`map --view hotspots`, `review`). A test
(`test_real_verinoda_executes_nothing_of_the_repository`) runs the real CLI, every step group
including `doctor`, `map`, the MCP probe, `update` and `review`, on a repository whose
`sitecustomize.py`, `conftest.py`, `setup.py`, `__main__.py` and a folder named `verinoda/` would
each leave a marker file if executed.

## How to add a repository

1. Pick one with a permissive license (MIT, Apache-2.0, BSD), over 10k stars, a recent release and
   about 100-1000 files. Pin its release tag and the commit sha (`git ls-remote URL refs/tags/TAG^{}`).
2. Add it to `manifest.json` with the star count and today's date. A `scenario` block is optional:
   `queries` (3), `analyze` (2), `trace` ([source, target], each naming one symbol: use
   `path/file::Name` when a name is defined twice), `q` (2), `edit` ({file, after_line, insert}: one
   line that is valid in that language), `check_file` (a `.py`, `.java` or `.kt` file). Without it the
   generic questions are used and the edit is a comment line appended to the largest source file.
3. Clone it (`run.py` does) and write `gold/<owner>__<name>.json` by reading the source: 5-10 facts,
   each with `source` (file:line) and a `check` of kind `q` (`cites`: the file:line an `at` of the
   rows must be, exactly), `trace` (`via_at`: file:line of edges of one path, `via_files`: files
   where an edge of that path is, `source_at` / `target_at`: where the ends must resolve), `query`
   (`expect_file` in the `top_k`), `routes` (method, path, handler) or `schema` (table). Commit the
   gold file before running Verinoda on that repository.
4. Run it alone: `python benchmarks/realworld/run.py --repos owner/name`.

A gold fact is never edited after the first run. A check written badly gets a new entry appended
with `"v": 2`, an id `<old id>@v2`, `replaces`, `added` and `why`; the report counts v1 (frozen,
comparable across runs) and v2 separately.
