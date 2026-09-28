# D66-index-speed (pending doc text for the operator to merge)

Section numbers are left as `NN` for the operator (the next free DESIGN.md section).

## DESIGN section

## NN. Faster scans: the file list from git, one parse, one commit, a renamed search build (D66, 2026-09-28)

### NN.1 Why

A profile of a full scan of a django checkout (6,653 tracked files, 2,888 Python) put the time outside the
tree-sitter pool: detect() spent about 18 s (profiled) evaluating every .gitignore rule per path in Python,
taking a realpath per file and asking git for the tracked files anyway, and it ran again on every update
(6 s of a one-edit update). Python files were parsed 9,209 times for 2,888 files: the receiver sidecar parsed
1,585 of them outside the per-version parse cache that the search index's span lookups use, so the search
index parsed them again. The anchors pass read and hashed each file twice and committed once per file. A full
search-index build deleted `search.db` first and wrote the new one through the ordinary WAL connection, so a
reader could meet a half-built index and a failed build left none.

### NN.2 Decisions

- detect() (vendored `project_index/detect.py`, hand-edited and marked "Verinoda patch") takes its file
  list from git when the scan root is the top of a git work tree, .gitignore rules are in play at the root
  and symlinks are not followed: `git ls-files --cached` (tracked files, which gitignore rules never drop)
  plus `--others --exclude-per-directory=.gitignore --exclude-from=<info/exclude>`, the rules the walk
  applies; the user's global excludes file is not passed because the walk never read it. Everything else
  the walk decides is still decided the same way: noise dirs and the output dir are pruned whatever git says,
  `.graphifyignore` and `--exclude` rules (root chain and each directory's own file) are evaluated per
  directory and per file, skipped names, sensitive files, and links: an lstat per file (the realpath check
  only for a link), and a realpath once per directory, because git lists files through a Windows junction
  as through a directory while the walk's per-file realpath check kept a junction out of the root out. The
  walk still runs for a nested repository or an untracked worktree (git lists
  it as `dir/`), a `.gitmodules` file, a `!` rule in .graphifyignore (it may re-include a file .gitignore
  drops, which git never lists), a git failure, or `enumeration="walk"`. The result says which ran
  (`"enumeration"`); `ignored` on the git path is git's report of what .gitignore dropped (a wholly ignored
  directory as one entry), which may group entries differently from the walk. Word counts stay (they feed
  GRAPH_REPORT.md and are stat-cached).
- `refresh_receiver_sidecar` parses through the per-version cache (`_PYINFO_CACHE`), so the span lookups
  of the search index that runs next in the same process reuse its parses.
- `anchors.update_facts` computes each file's facts from the bytes it already read (it read and hashed the
  file again through `facts_for`) and writes every new row in one transaction
  (`Store.put_file_facts_many`); `facts_for` is unchanged for one-off callers.
- A from-scratch search index is written to `search.db.build-<pid>` with no journal, no syncs and a 64 MB
  page cache, switched to WAL, and renamed over `search.db` after the old index's `-wal` and `-shm` files are
  removed (a WAL left next to the new file would be replayed into it). Where the old file is held open and
  cannot be replaced (Windows), the pages are copied into it with SQLite's backup API. A failed build leaves
  the old index; build files a killed build left are removed after an hour.
- Not done: lexicon and anchors still parse Python separately (report item 6b), the three derived passes
  still run one after the other (6e), and definition spans are not stored in graph nodes (item 9). Every
  update still builds `ignored_predicate(gitignore=True)` in `watch._rebuild_code` for its reconcile step,
  which runs `git ls-files` and evaluates the .gitignore rules for the graph's files: part of the update
  cost the report put on detect() remains there. The parse sharing of the sidecar is bounded by
  `index._CACHE_MAX` (4,096 entries, cleared wholesale): a repository with more Python files than that
  gets part of the gain (an existing limit).

### NN.3 Measured

Machine shared with other builds (CPU load 76-96 % throughout); every wall time is an upper bound and
replicates differ by up to 2x. The django checkout was copied without its `.verinoda` folder.

- Same corpus: `files`, `total_words` and `unclassified` (what the rebuild reads from detect()) identical,
  walk vs git, on seven trees: the django checkout, hono (TS), sidekiq (Ruby), AutoMapper (C#), jq (C; it
  has submodules, so the walk ran), the orders_app example with an ignored directory, an ignored file, an
  untracked file and a tracked noise dir added, and this repository as a linked worktree (a `.git` file).
- detect() on the django checkout, warm, under the profiler: 17.7 s (walk) -> 2.3 s (git). Unprofiled, two
  runs each: walk 16.7 / 18.7 s, git 6.1 / 2.6 s (loaded box). hono 1.6 / 1.2 -> 0.9 / 1.3 s; sidekiq
  0.8 / 0.4 -> 1.1 / 0.5 s (small trees: within noise).
- Search index, full build of the django checkout, old vs new code, two runs each: SEARCH_NUMBERS.
- `verinoda scan` and a one-line-edit `update` of the django checkout, two runs each (scan, update 1,
  update 2; wall seconds): before BEFORE_NUMBERS; after AFTER_NUMBERS.
- Tests: `tests/test_detect_git.py` (git list == walk on a repository with tracked-but-ignored, nested
  .gitignore, info/exclude, .graphifyignore dir and file, --exclude, a deleted tracked file, a noise dir,
  the output dir, symlinks where available, a junction out of the root on Windows; the walk for a negation, a nested repository, .gitmodules, no
  .gitignore, a git failure), `tests/test_search_build.py` (every table of the renamed build equals a build
  through the ordinary connection and the default location, on two examples; a held index rebuilt through
  the backup API; a failed build leaves the old index), `tests/test_anchors.py`
  (`update_facts` stores the rows `facts_for` stores, in one commit, over every file of `examples/`),
  `tests/test_index.py` (the sidecar's parses serve the span lookups); `tests_upstream/test_detect.py`
  and the other detect tests pass unchanged (281 passed).

## Decision table row

| D66 | Faster scans | implemented | Built 2026-09-28 (section NN): detect() takes the file list from git at the top of a work tree (same corpus as the walk, measured on seven trees; the walk still runs for nested repositories, submodules, negated .graphifyignore rules), the receiver sidecar's Python parses serve the search index's spans, anchors are written in one transaction from one read, and a from-scratch search index is built in a separate file renamed into place. |

## UPGRADING note

- Nothing to run. `verinoda scan` / `update` list files through `git ls-files` in a git repository whose root
  has a `.gitignore`; the files indexed are the same. A file ignored only by the global git excludes file
  (`core.excludesFile`) is indexed as before (detect never read that file). The detect() result has a new key
  `enumeration` (`"git"` or `"walk"`), and on the git path its `ignored` list is git's report of what
  .gitignore dropped.
- A full search-index build writes `.verinoda/index/search.db.build-<pid>` and renames it to `search.db`
  when complete; a build that fails keeps the previous index (it used to be deleted first). A leftover
  build file from a killed build is removed by the next full build after an hour, or can be deleted by hand.

## UPSTREAM.md note (docs/UPSTREAM.md, "Modified" table and the "No file ... is hand-edited" sentence)

`project_index/detect.py` is now hand-edited: `_git_listed_files`, `_git_enumerate` and the `enumeration`
parameter of `detect()` (marked "Verinoda patch"; the Office-files branch was already a local edit). A
re-sync with `tools/port_upstream.py` overwrites the file: re-apply the patch and run
`tests/test_detect_git.py` and `tests_upstream/test_detect.py`.
