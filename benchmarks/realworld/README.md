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
                     on that repository
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

Each repository is cloned shallow at its tag (an existing clone at the pinned sha is reused; a folder
at another sha is an error and is left alone), then: `init` (cold: an old `.verinoda/` is removed),
`scan`, the gold checks, 3 `query`, 2 `analyze`, `trace`, 2 `q`, `check` on one file, `map` views
(dependencies, dataflow, dead, hotspots), `routes`, `schema`, `taint` (Python repositories), `doctor`,
the MCP tools `project_query` and `analyze`, then a scripted one-line edit, `update`, `review`, the
edit reverted byte for byte, and `update` again. Every command is `python -P -m verinoda ...` with
`PYTHONPATH` set to the checkout under test, so a folder of the corpus named like a package is never
imported.

A step is a crash when its stderr holds a Python traceback or its exit code is not one the command
documents (the table `OK_CODES` in run.py); a timeout when it runs past its limit (`TIMEOUTS`, the
process tree is killed). The run exits 1 when a repository had a crash, a timeout or could not be
cloned.

Results: `results.json` (per repository: every step's argv, seconds, exit code, stdout and stderr
sizes, whether the JSON parsed, the stderr tail; the gold facts with hit or miss and why) and
`summary.md`. A repository run again on the same day replaces its entry. Paths are written as
`<CORPUS>`, `<REPO>` and `<HOME>`.

## What is never executed

The repository's code, tests, build and tools. `run.py` refuses, before any process starts, the
commands `trust`, `observe`, `experiment`, `probe`, `mutate`, `setup`, `hooks`, `install`,
`agent-hooks`, `ui`, `research`, `compare`, `index` and the flags `--run-tests`, `--observe`,
`--checker`, `--env`, `--deps`, `--registry` (see `guard_argv`). The MCP `analyze` is called with
`run_tests=False, observe=False`. `check` uses Verinoda's own interpreter with the standard library
only (no environment of the repository is created). `git` is used for clone, rev-parse and ls-files;
Verinoda itself reads the history (`map --view hotspots`, `review`). A test
(`test_real_verinoda_executes_nothing_of_the_repository`) runs the real CLI on a repository whose
`sitecustomize.py`, `conftest.py`, `setup.py`, `__main__.py` and a folder named `verinoda/` would
each leave a marker file if executed.

## How to add a repository

1. Pick one with a permissive license (MIT, Apache-2.0, BSD), over 10k stars, a recent release and
   about 100-1000 files. Pin its release tag and the commit sha (`git ls-remote URL refs/tags/TAG^{}`).
2. Add it to `manifest.json` with the star count and today's date. A `scenario` block is optional:
   `queries` (3), `analyze` (2), `trace` ([source, target]), `q` (2), `edit` ({file, after_line,
   insert}: one line that is valid in that language), `check_file`. Without it the generic questions
   are used and the edit is a comment line appended to the largest source file.
3. Clone it (`run.py` does) and write `gold/<owner>__<name>.json` by reading the source: 5-10 facts,
   each with `source` (file:line) and a `check` of kind `q` (`cites`: file:line the rows must name),
   `trace` (`via_files`), `query` (`expect_file` in the `top_k`), `routes` (method, path, handler) or
   `schema` (table). Commit the gold file before running Verinoda on that repository.
4. Run it alone: `python benchmarks/realworld/run.py --repos owner/name`.
