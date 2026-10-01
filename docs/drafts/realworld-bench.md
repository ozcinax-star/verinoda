# Real-world benchmark and end-user tests

Verinoda run the way a user runs it, on pinned open-source repositories it was not built on. The
harness is in `benchmarks/realworld/` (runner, report, manifest, gold facts) and its tests are in
`tests/test_realworld_harness.py`. The first measured results, on the two smallest repositories,
are in `benchmarks/results/realworld-2026-10-01/`.

## Why

The existing benchmarks measure retrieval on three corpora: `examples/orders_app`, upstream
Graphify, and Verinoda's own older source. All three are Python, and two of them were known while
the tool was being built. This leaves two questions open:

- does every user-facing command finish, without a traceback and in reasonable time, on code
  nobody tuned for?
- does it get simple facts right in other languages and other projects' layouts?

This harness answers both for ten repositories (Python, Python + TypeScript, JavaScript,
TypeScript, Go, Rust, Java and PHP). Each one has a permissive license, more than 10k stars and a
recent release, and each is pinned to a tag and sha.

## Decisions

- **Pinned, not latest.** The manifest gives each repository a tag and the sha it resolved to on
  2026-10-01, plus the star count on that date. The clone is shallow at that tag, and the runner
  stops if `HEAD` is not the pinned sha. If a folder in the work directory is at another sha, the
  runner reports an error and leaves the folder as it is.
- **Clones outside OneDrive.** The default work directory is `C:/vbench/<owner>__<name>`. It can be
  changed with `--work` or `$VERINODA_BENCH_WORK`, and the runner warns when the folder is under
  OneDrive. Sync would slow indexing down and lock files.
- **The repository's code is never executed.** `guard_argv` refuses, before any process starts:
  - the commands `trust`, `observe`, `experiment`, `probe`, `mutate`, `setup`, `hooks`, `install`,
    `agent-hooks`, `ui`, `research`, `compare` and `index`;
  - the flags `--run-tests`, `--observe`, `--checker`, `--env`, `--deps` and `--registry`.

  The MCP `analyze` call passes `run_tests=False` and `observe=False`. `check` runs with Verinoda's
  own interpreter and only the standard library. Every command runs as `python -P -m verinoda` with
  `PYTHONPATH` set to the checkout under test. Neither the corpus's working directory nor its
  `sitecustomize.py`, nor a corpus folder named `verinoda/`, is ever imported. A test checks this
  with the real CLI and marker files.
- **Every step is a subprocess.** The runner records its seconds, exit code, stdout and stderr
  sizes, whether the JSON parsed, and the tail of stderr. A step counts as a crash in either case:
  - its stderr holds a Python traceback (even when the exit code is 0);
  - its exit code is not one the command documents (`OK_CODES`: for example `trace` 2 means no
    path, `q` 1 means no row, `check` 4 means a file was not checked).

  A step that runs past its limit (`TIMEOUTS`) is a timeout, and its whole process tree is killed
  (`taskkill /T` on Windows).
- **Scenarios a user would run.** The runner does these in order:
  1. `init` (cold) and `scan`;
  2. the gold checks;
  3. 3 `query`, 2 `analyze`, `trace`, 2 `q`, and `check` on one file;
  4. four `map` views, `routes`, `schema`, `taint` (Python repositories only) and `doctor`;
  5. MCP `project_query` and `analyze` through `AtlasTools`;
  6. a one-line edit, then `update`, `review`, a byte-exact revert, and `update` again.

  The questions and the edit come from the manifest's `scenario` when there is one. Otherwise the
  runner uses generic questions and appends a comment line to the largest source file.
- **Gold facts are frozen before the run.** For each repository I read 5-10 facts by hand in the
  clone: a definition at file:line, X calls Y at file:line, a path through the code, or the file
  that answers a question. Each fact names the Verinoda command that checks it (`q` with the
  file:line the rows must cite, `trace` with the files the path must reach, `query` with the file
  that must be in the top k, or `routes`/`schema`). I committed them before Verinoda first ran on
  that repository: commit 73041f4 has the gold files, and the runs came after 27f72bf. A miss stays
  a miss. A check written badly is fixed in a new gold version, not by editing the frozen one.
- **Results without machine paths.** Paths are written as `<CORPUS>`, `<REPO>` and `<HOME>`. If a
  repository is run again on the same day, its new entry replaces the old one in that day's
  `results.json`.

## Measured

Run on 2026-10-01 with Verinoda 0.3.2 at 27f72bf, Python 3.13.14, Windows 11, 16 CPUs. One process
ran at a time, and times are wall clock.

| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | gold |
|---|---:|---:|---:|---:|---:|---:|---:|
| gin-gonic/gin v1.12.0 (Go) | 130 | 12.0 | 7.9 | 1.4 / 2.6 | 0 | 0 | 7/10 |
| junegunn/fzf v0.74.4 (Go) | 161 | 16.7 | 8.5 | 1.5 / 3.5 | 0 | 0 | 9/10 |

- **No crashes and no timeouts** in any of the 33 steps per repository (10 of them gold checks).
- **Exit codes.** `check` on a Go file exits 4: the file was not checked because it is another
  language, which the command documents. `review` exits 3 because it found something to report.
- **Largest output.** `map --view dead` was the largest response: 149 KB for gin and 141 KB for fzf
  in JSON. Every other step stayed under 15 KB.
- **The scripted edits.** gin got `_ = engine` inside `Default`, and fzf got `_ = opts` inside
  `Run`. Both were reverted byte for byte, and `git status` was clean afterwards.
- **The 4 misses, read one by one.**
  - **gin `request-reaches-tree` and `get-registers-route`.** The graph has no edge for a Go method
    called on a local variable (`root.getValue`, gin.go:715) or on a field (`group.engine.addRoute`,
    routergroup.go:89). `q` lists the other callees of `handleHTTPRequest` and of
    `RouterGroup.handle`, but not these two. The receiver's type is not inferred for Go.
  - **gin `default-calls-recovery`.** `trace Default Recovery` stops with "ambiguous", because both
    `gin.Default` and `binding.Default` exist. Refusing to guess is the right behaviour. The check
    should have named `gin.go#Default`, and that fix goes in the next gold version.
  - **fzf `main-calls-run`.** The edge is in the graph: `q` returns `main() -calls-> Run()
    (main.go:113)`. But `trace main.go src/core.go` finds no directed path, because a trace between
    two files does not follow calls between the functions they contain. That is a gap in trace,
    and the frozen check stays a miss.
- **What hit.** All four `query` facts were at rank 1. All definitions, and every direct call
  between named functions or methods on the receiver, were hits.

## Limits

- Only 2 of the 10 repositories have been run, and both are Go. The Python, JavaScript/TypeScript,
  Rust, Java and PHP repositories, and the Python-only steps (`taint`, and `check`'s name checks),
  have not been measured on real code yet. The full run comes later.
- Each fact was checked by one reader, me. There are 10 facts per repository, so a single fact
  moves the score by 10 points. Gold measures recall of simple facts, not precision: a wrong extra
  edge does not count against Verinoda.
- Times are from one run on one machine, without repeats, so they carry no variance.
- The edit is a single line, so `update` and `review` are measured on the smallest possible
  change.
- The crash rule depends on the documented exit codes. If a command returns a wrong answer with an
  allowed code, the rule does not see it. Only the gold facts see wrong answers.
- Git and Verinoda read the clone, and `git log` reads its history. Clones are made with our own
  git config, and no hooks or filters come from the repository.

## Upgrading note

This adds no change to Verinoda itself. There are new files under `benchmarks/realworld/`, new
results under `benchmarks/results/realworld-<date>/` and a new test module. Two things to know
before running:

- **Clone folder.** By default, running it clones into `C:/vbench`, which took 30 MB for the two
  small repositories (clones and their `.verinoda/`) and a few hundred MB for all ten.
- **Network.** Cloning needs the network. The tests do not.

## Tests

`tests/test_realworld_harness.py` has 33 tests. They need no network and clone nothing from
GitHub: the clone step is mocked, and `head_sha` is tested on a local `git init` repository. Most
of them drive the runner with a fake `-m fakevn` module that answers, crashes, hangs or exits with
an undocumented code on demand. They cover:

- the guard, for each forbidden command and flag, refusing before any process is started;
- a full scenario whose argvs are all logged and checked against the guard;
- a traceback counted as a crash even with exit 0, an undocumented exit counted as a crash, and a
  documented one not counted;
- a timeout that kills the process within the limit;
- path sanitizing and JSON detection;
- the summary's crash, timeout and gold counts, the cold `init`, and the byte-exact revert;
- gold judging for every kind, with exact line matching (236 does not match 2360);
- well-formed gold files whose sha matches the manifest;
- the clone being reused, and a foreign sha refused with the folder kept;
- an edit on a CRLF file;
- the report table, `merge_results`, and the report CLI.

One test runs the real Verinoda (`init`, `scan`, a gold `q`, `check`, `taint`) on a tiny
repository. That repository's `sitecustomize.py`, `usercustomize.py`, `conftest.py`, `setup.py`,
`pkg/__main__.py` (which the code imports) and a `verinoda/` package would each leave a marker
file if executed. The test asserts that no marker appears, that there are no crashes, and that
the gold hit is found.
