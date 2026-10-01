# Real-world benchmark and end-user tests

Verinoda run the way a user runs it, on pinned open-source repositories it was not built on. The
harness is in `benchmarks/realworld/` (runner, report, manifest, gold facts) and its tests are in
`tests/test_realworld_harness.py`. The first measured results, on the two smallest repositories,
are in `benchmarks/results/realworld-2026-10-01/` (rerun after a review round).

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
- **The repository's code is never executed.** `guard_argv` is an allow list. Only `init`, `scan`,
  `update`, `query`, `analyze`, `trace`, `q`, `check`, `map`, `routes`, `schema`, `taint`, `review`
  and `doctor` may start, each with its own options among `--json`, `--view`, `--max-items` and
  `--mode`. Any other command and any other token starting with `-` is refused before any process
  starts, abbreviations included.

  The MCP `analyze` call passes `run_tests=False` and `observe=False`. `check` runs with Verinoda's
  own interpreter and only the standard library. Every command runs as `python -P -m verinoda` with
  `PYTHONPATH` set to the checkout under test. Neither the corpus's working directory nor its
  `sitecustomize.py`, nor a corpus folder named `verinoda/`, is ever imported. Each repository's
  steps see a fresh, empty `VERINODA_CONFIG_DIR`, so the user's `trust.json` never makes a clone
  trusted. A test checks all this with the real CLI and marker files.
- **Every step is a subprocess.** The runner records its seconds, exit code, stdout and stderr
  sizes, whether the JSON parsed, and the tail of stderr. A step counts as a crash in any of these
  cases:
  - its stderr holds a Python traceback (even when the exit code is 0);
  - its exit code is not one the command documents (`OK_CODES`: for example `trace` 2 means no
    path, `q` 1 means no row). Gold checks use the codes of their own command;
  - a non-zero documented exit comes without a JSON object (for `q`, one with `rows`). Verinoda's
    internal errors also exit 1, with `error: ...` and no JSON;
  - for the MCP probe, the tool answered `{"error": ...}`.

  A step that runs past its limit (`TIMEOUTS`) is a timeout, and its whole process tree is killed
  (`taskkill /T` on Windows).
- **Scenarios a user would run.** The runner does these in order:
  1. `init` (cold) and `scan`;
  2. the gold checks;
  3. 3 `query`, 2 `analyze`, `trace`, 2 `q`, and `check` on one Python, Java or Kotlin file
     (skipped and recorded when the repository has none);
  4. four `map` views, `routes`, `schema`, `taint` (Python repositories only) and `doctor`;
  5. MCP `project_query` and `analyze` through `AtlasTools`;
  6. a one-line edit, then `update`, `review`, a byte-exact revert, `update` again, and a check
     that no tracked file changed.

  The questions and the edit come from the manifest's `scenario` when there is one. Otherwise the
  runner uses generic questions and appends a comment line to the largest source file.
- **Gold facts are frozen before the run.** For each repository I read 5-10 facts by hand in the
  clone: a definition at file:line, X calls Y at file:line, a path through the code, or the file
  that answers a question. Each fact names the Verinoda command that checks it (`q` with the
  file:line the rows must cite, `trace` with the edges the path must have, `query` with the file
  that must be in the top k, or `routes`/`schema`). I committed them before Verinoda first ran on
  that repository: commit 73041f4 has the gold files, and the runs came after 27f72bf. A miss stays
  a miss. A check written badly is fixed in a new gold version, not by editing the frozen one.
- **Results without machine paths.** Paths are written as `<CORPUS>`, `<REPO>` and `<HOME>`. If a
  repository is run again on the same day, its new entry replaces the old one in that day's
  `results.json`.
- **Review round.** A review of the first version found eight problems. Each fix has a regression
  test.
  1. **Guard.** The deny list let abbreviations (`--run-test`, `--obs`) and code-running commands
     (`debug try`, `run`, `spec`, `mutations`) through. It is now the allow list above.
  2. **Hidden errors.** An internal error with exit 1 counted as an answer for `q` and `doctor`,
     and as a plain miss for gold checks. It is now a crash.
  3. **MCP errors.** An MCP `{"error": ...}` counted as a success. The probe now exits 1.
  4. **Gold checks that passed for the wrong reason.**
     - `cites` also matched `binding/gin.go:236` for `gin.go:236`. It now reads only `at` fields,
       exactly.
     - A trace's `via_files` could be met by the resolved target. Now the via files must be where
       an edge of one path is, and both ends must be resolved.
     - fzf `pattern-matchitem` cited the definition line. Its v2 cites the call at
       src/pattern.go:395.
  5. **One error lost the whole run.** Each repository is now wrapped and recorded on its own.
     Results are written after each repository, and the edit works on bytes.
  6. **Dirty clones.** A dirty clone is now refused, and the clone is checked clean after the
     revert.
  7. **Symlinks.** The edit could write through a symlink. It now skips symlinks and paths
     outside the clone. Clones are made without symlinks and LFS downloads, with long paths
     allowed.
  8. **Environment.** The latest run's environment used to relabel earlier repositories. Each
     repository and each run now keeps its own.

  Minor fixes in the same round:
  - a fresh config folder, so the user's trust never applies;
  - a check that Python is 3.11 or later;
  - a tracked `.verinoda/` is kept;
  - half-finished clones are retried;
  - trace pairs that name one symbol (`gin.go::Default`, `main.go::main`);
  - `check` only on a language it reads.

  Five gold v2 entries were appended. The v1 facts are unchanged and still scored, and the report
  counts v1 and v2 apart.

## Measured

Run on 2026-10-01 after the review round, with Verinoda 0.3.2 at b4b6b5a (clean), Python 3.13.14,
Windows 11 and 16 CPUs. One process ran at a time, and times are wall clock.

| repo | files | scan s | update s | query / analyze median s | crashes | timeouts | clean after | gold v1 | gold v2 |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| gin-gonic/gin v1.12.0 (Go) | 130 | 11.9 | 7.0 | 1.4 / 2.6 | 0 | 0 | yes | 7/10 | 1/2 |
| junegunn/fzf v0.74.4 (Go) | 161 | 14.5 | 7.5 | 1.5 / 3.4 | 0 | 0 | yes | 8/10 | 3/3 |

- **No crashes and no timeouts** under the stricter rules: 34 steps for gin and 35 for fzf (12 and
  13 of them gold checks). `check` was skipped on both, because neither has a Python, Java or
  Kotlin file. In the first run, `check` on a Go file exited 4 and measured nothing.
- **Both clones were clean** after the revert. The runner checked this with `git status`.
- **The scenario traces now resolve.** `trace gin.go::Default LoggerWithConfig` and
  `trace main.go::main NewMatcher` both exited 0 with status `found`. In the first run, both pairs
  exited 2: one was ambiguous and the other found no path.
- **Other exit codes.** gin's `q2` (who calls `Next`) exited 1 with a JSON answer and no row.
  `review` exited 3 on both, because it found something to report.
- **Largest output.** `map --view dead` was the largest response: 149 KB for gin and 141 KB for fzf.
  Every other step stayed under 15 KB.
- **v1 gold under the exact rules.** gin stays at 7/10. fzf drops from 9/10 to 8/10, because
  `run-calls-newmatcher` had hit only through the resolved target: its one edge is at
  src/core.go:258, not in src/matcher.go. The other misses are the same as in the first run:
  - gin `default-calls-recovery` (`Default` is ambiguous);
  - gin `request-reaches-tree` and `get-registers-route` (no Go edge for a method called on a local
    variable or a field: `root.getValue`, gin.go:715, and `group.engine.addRoute`,
    routergroup.go:89);
  - fzf `main-calls-run` (trace between two files).
- **v2 gold.** 4 of 5 hit:
  - gin `default-calls-recovery@v2` (edge at gin.go:239);
  - fzf `main-calls-run@v2` (main.go:113);
  - fzf `run-calls-newmatcher@v2` (src/core.go:258);
  - fzf `pattern-matchitem@v2` (src/pattern.go:395).

  gin `request-reaches-tree@v2` still finds no directed path, for the receiver-type gap described
  above.

## Limits

- Only 2 of the 10 repositories have been run, and both are Go. The Python, JavaScript/TypeScript,
  Rust, Java and PHP repositories, and the steps that need those languages (`taint`, and `check`,
  which was skipped on both), have not been measured on real code yet. The full run comes later.
- Each fact was checked by one reader, me. There are 10 v1 facts per repository, so a single fact
  moves the score by 10 points. Gold measures recall of simple facts, not precision: a wrong extra
  edge does not count against Verinoda. The v2 entries were written after the first run, so they
  are not blind; they correct how a fact is checked, never the fact.
- Times are from one run on one machine, without repeats, so they carry no variance.
- The edit is a single line, so `update` and `review` are measured on the smallest possible
  change.
- The crash rule depends on the documented exit codes and on the JSON a non-zero exit must carry.
  If a command returns a wrong answer with an allowed code, the rule does not see it. Only the gold
  facts see wrong answers.
- Git and Verinoda read the clone, and `git log` reads its history. Clones are made with our own
  git config (no symlinks, no LFS download), and no hooks or filters come from the repository.
- The shas of the 8 repositories not run yet were not checked offline; `ensure_clone` stops on a
  mismatch.

## Upgrading note

This adds no change to Verinoda itself. There are new files under `benchmarks/realworld/`, new
results under `benchmarks/results/realworld-<date>/` and a new test module. Three things to know
before running:

- **Clone folder.** By default, running it clones into `C:/vbench`, which took 30 MB for the two
  small repositories (clones and their `.verinoda/`) and a few hundred MB for all ten.
- **Network.** Cloning needs the network. The tests do not.
- **Python.** The runner needs Python 3.11 or later (`python -P`) and refuses an older one.

## Tests

`tests/test_realworld_harness.py` has 87 tests (one of them skips where symlinks cannot be made,
as on this Windows account). They need no network and clone nothing from GitHub: the clone step is
mocked, and the git parts run on local `git init` repositories. Most of them drive the runner with
a fake `-m fakevn` module that answers, crashes, hangs, exits 1 with `error:` or with a JSON
answer, or exits with an undocumented code on demand. They cover:

- the allow list: forbidden and missing commands, abbreviations (`--run-test`, `--obs`), options of
  other commands, `debug try`, `run`, `spec`, `mutations`; the scenario argvs let through; nothing
  started when an argv is refused;
- a full scenario whose argvs are all logged and checked against the allow list;
- a traceback counted as a crash even with exit 0, an undocumented exit counted as a crash, and a
  documented one not counted; exit 1 without JSON a crash for `q`, `doctor` and gold steps; `q`
  exit 1 with `rows` an answer;
- the MCP probe exiting 1 on `{"error": ...}`;
- a timeout that kills the process within the limit;
- path sanitizing and JSON detection;
- the summary's crash, timeout and v1/v2 gold counts, the cold `init`, and the byte-exact revert;
- gold judging for every kind: exact `at` matching (236 does not match 2360, `binding/gin.go:236`
  does not match `gin.go:236`); trace via files on path edges of one path, both ends resolved and
  located; JSON that is not an object;
- well-formed gold files (v1 frozen, v2 entries with `replaces` and `why`) whose sha matches the
  manifest, and scenarios that measure something;
- the clone: reused, a foreign sha refused with the folder kept, a half-finished clone removed and
  cloned again, the git arguments (no symlinks, long paths, no LFS);
- a dirty clone refused, a clone left dirty after the revert reported, a tracked `.verinoda/` kept;
- the edit: CRLF, Latin-1 bytes, and never through a symlink or outside the clone;
- the empty config folder, and a clone the user's config trusts that is not trusted under the
  runner;
- one failing repository recorded while the next one runs, with results on disk after each; the
  environment per run and per repository; Python older than 3.11 refused;
- the report table, `merge_results`, and the report CLI.

One test runs the real Verinoda on a tiny git repository, through every step group (`init`,
`scan`, a gold `q`, `trace`, `check`, `map`, `taint`, `doctor`, the MCP probe, the edit with
`update` and `review`). That repository's `sitecustomize.py`, `usercustomize.py`, `conftest.py`,
`setup.py`, `pkg/__main__.py` (which the code imports) and a `verinoda/` package would each leave a
marker file if executed. The test asserts that no marker appears, that there are no crashes, that
the gold hit is found and that the clone is clean afterwards.
