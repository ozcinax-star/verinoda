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
  applies; the user's global excludes file is not passed because the walk never read it, and the directory
  names the walk prunes unconditionally (`_SKIP_DIRS`: node_modules, venv, build, ...) are passed as
  `--exclude=<name>/` so git does not enumerate an un-ignored one only for its files to be discarded.
  The .gitignore rules are git's own, which the Python matcher of the walk does not reproduce everywhere:
  its `*` crosses `/` (`test*.py` drops `tests/foo.py`; git keeps it), it folds case where the file
  system does while git follows `core.ignorecase`, and a UTF-16 or ANSI-code-page .gitignore the walk
  decodes is not valid UTF-8 to git. So the corpus of the git path is git's view of the tree; the walk
  runs where the difference would be large (the encoding case, see below), and the reconcile step of
  a rebuild (`ignored_predicate(gitignore=True)`) never takes a matcher-only verdict as evidence to
  evict a file git lists as untracked and not ignored: the tracked-file exemption, extended to the
  files git keeps, so a rebuild that walks after one that listed through git keeps them. Every git call
  of detect.py drops the repository-local variables a git hook exports (`GIT_DIR`, `GIT_INDEX_FILE`,
  ...), which would make `git -C <root>` describe another repository or index. The rest of what
  the walk decides is still decided the same way: noise dirs and the output dir are pruned whatever git says,
  `.graphifyignore` and `--exclude` rules (root chain and each directory's own file) are evaluated per
  directory and per file, skipped names, sensitive files, and links: an lstat per file (the realpath check
  only for a link), and a realpath once per directory, because git lists files through a Windows junction
  as through a directory while the walk's per-file realpath check kept a junction out of the root out. The
  walk still runs for a nested repository or an untracked worktree (git lists
  it as `dir/`), a `.gitmodules` file or a gitlink without one (`git add` of a nested clone records an
  embedded repository that way, and git lists none of its files), a `!` rule in .graphifyignore (it may
  re-include a file .gitignore drops, which git never lists), a .gitignore or `info/exclude` that is not
  UTF-8, a directory git cannot open (the walk names it in `walk_errors` and warns; git only warns on
  stderr), a git failure, or `enumeration="walk"`. The directory checks run in a loop, not a recursion
  (a path can be deeper than Python's recursion limit). The result says which ran
  (`"enumeration"`); `ignored` on the git path is git's report of what .gitignore dropped (a wholly ignored
  directory as one entry), which may group entries differently from the walk. A file renamed only in
  case without `git mv` on a case-folding file system keeps the index's spelling on the git path (the
  walk has the disk's); the path opens either way there, and checking the disk spelling would cost a
  directory listing per directory on every scan. Word counts stay (they feed GRAPH_REPORT.md and are
  stat-cached).
- `refresh_receiver_sidecar` parses through the per-version cache (`_PYINFO_CACHE`), so the span lookups
  of the search index that runs next in the same process reuse its parses.
- `anchors.update_facts` computes each file's facts from the bytes it already read (it read and hashed the
  file again through `facts_for`) and writes every new row in one transaction
  (`Store.put_file_facts_many`); `facts_for` is unchanged for one-off callers.
- A from-scratch search index is written to `search.db.build-<pid>` with no journal, no syncs and a 64 MB
  page cache, switched to WAL, and renamed over `search.db` after the old index's `-wal` and `-shm` files are
  removed (a WAL left next to the new file would be replayed into it). Where the old file is held open and
  cannot be replaced (Windows), the pages are copied into it with SQLite's backup API. A failed build leaves
  the old index; build files a killed build left are removed after an hour. The build file is opened for
  installing without creating it (`mode=rw`) and must hold the index's `meta` table: a build file the
  hour sweep removed while it was still being written (POSIX lets a live file be unlinked) fails the
  build and keeps the old index instead of installing an empty database.
- Not done: lexicon and anchors still parse Python separately (report item 6b), the three derived passes
  still run one after the other (6e), and definition spans are not stored in graph nodes (item 9). Every
  update still builds `ignored_predicate(gitignore=True)` in `watch._rebuild_code` for its reconcile step,
  which runs `git ls-files` and evaluates the .gitignore rules for the graph's files (and, the first time
  the rules alone drop a file, `git ls-files --others` to see whether git keeps it): part of the update
  cost the report put on detect() remains there. The parse sharing of the sidecar is bounded by
  `index._CACHE_MAX` (4,096 entries, cleared wholesale): a repository with more Python files than that
  gets part of the gain (an existing limit).

### NN.3 Measured

Machine shared with other builds (CPU load 88-100 % throughout); every wall time is an upper bound and
replicates differ by up to 2x. The django checkout was copied without its `.verinoda` folder.

- Same corpus on the trees measured (not in general: see the matcher differences in NN.2): `files`,
  `total_words` and `unclassified` (what the rebuild reads from detect()) identical, walk vs git, on seven
  trees: the django checkout, hono (TS), sidekiq (Ruby), AutoMapper (C#), jq (C; it
  has submodules, so the walk ran), the orders_app example with an ignored directory, an ignored file, an
  untracked file and a tracked noise dir added, and this repository as a linked worktree (a `.git` file).
- detect() on the django checkout, warm, under the profiler: 17.7 s (walk) -> 2.3 s (git). Unprofiled, two
  runs each: walk 16.7 / 18.7 s, git 6.1 / 2.6 s (loaded box). hono 1.6 / 1.2 -> 0.9 / 1.3 s; sidekiq
  0.8 / 0.4 -> 1.1 / 0.5 s (small trees: within noise).
- `verinoda scan` and then two one-line-edit `update`s of a fresh django copy, twice per code version, each
  from a frozen copy of the code. Wall seconds (scan / update 1 / update 2): before 281.9 / 90.0 / 110.2
  and 302.9 / 81.5 / 64.5; after 276.8 / 156.7 / 127.2 and 246.1 / 141.5 / 117.1. Phase seconds of the
  scans (index / search / lexicon / anchors): before 124.8 / 55.0 / 20.3 / 64.6 and 108.1 / 81.9 / 19.9 /
  78.6; after 154.3 / 55.0 / 27.9 / 11.7 and 146.5 / 33.5 / 31.4 / 18.6. Index phase of the updates:
  before 68.7, 61.3, 67.3, 45.8; after 102.0, 68.6, 116.1, 68.5.
- Reading: the box was at 100 % CPU with about 28 Python processes of other builds during the after runs
  (the before runs overlapped this build's own test runs instead), so the walls and the index phase are
  load, not code: the lexicon phase, which this change does not touch, is 40-55 % slower in the after runs.
  The one phase the load cannot explain is anchors: 64.6 / 78.6 s -> 11.7 / 18.6 s (one read, one commit).
  The search phase (55.0 / 81.9 -> 55.0 / 33.5 s) mixes the renamed build with the sidecar's parse reuse;
  the build file's effect alone was not isolated (an alternating build-only run was stopped because the
  box did not free up). The detect() gain shows only in the profile above: the scan JSON has no detect
  phase. An idle-box rerun of these two benches is needed before quoting end-to-end numbers. (The after
  runs used the code before the once-per-directory junction check was added: one realpath per kept
  directory, about 700 on django.)
- Tests: `tests/test_detect_git.py` (git list == walk on a repository with tracked-but-ignored, nested
  .gitignore, info/exclude, .graphifyignore dir and file, --exclude, a deleted tracked file, a noise dir,
  the output dir, symlinks where available, a junction out of the root on Windows, an un-ignored
  node_modules holding a repository, inherited `GIT_DIR`/`GIT_INDEX_FILE`; the walk for a negation, a
  nested repository, .gitmodules, an embedded repository (gitlink without .gitmodules), a UTF-16 and an
  ANSI .gitignore, an unreadable directory, no .gitignore, a git failure; a 1,100-level path; the ignore
  predicate keeps what git keeps, and a full rebuild that walks after one through git evicts nothing
  git keeps), `tests/test_search_build.py` (every table of the renamed build equals a build
  through the ordinary connection and the default location, on two examples; a held index rebuilt through
  the backup API; a failed build leaves the old index; a build file removed before it is installed fails
  the build and keeps the old index), `tests/test_line_endings.py` (no CR bytes in the package and its
  tests), `tests/test_anchors.py`
  (`update_facts` stores the rows `facts_for` stores, in one commit, over every file of `examples/`),
  `tests/test_index.py` (the sidecar's parses serve the span lookups); `tests_upstream/test_detect.py`
  and the other detect tests pass unchanged (281 passed).

## Decision table row

| D66 | Faster scans | implemented | Built 2026-09-28 (section NN): detect() takes the file list from git at the top of a work tree (git's own .gitignore semantics; the same corpus as the walk on the seven trees measured, known differences listed; the walk still runs for nested and embedded repositories, submodules, negated .graphifyignore rules, non-UTF-8 ignore files, unreadable directories; a rebuild never evicts a file git keeps), the receiver sidecar's Python parses serve the search index's spans, anchors are written in one transaction from one read, and a from-scratch search index is built in a separate file renamed into place. |

## UPGRADING note

- Nothing to run. `verinoda scan` / `update` list files through `git ls-files` in a git repository whose root
  has a `.gitignore`, which applies git's own .gitignore semantics. On most trees the files indexed are the
  same; where Verinoda's matcher read a rule differently from git, git's reading now counts: a `*` does
  not cross `/` (`test*.py` no longer drops `tests/foo.py`), case follows `core.ignorecase`, and a file
  renamed only in case without `git mv` keeps git's spelling on a case-folding file system. Such a file
  is added by the next scan and never evicted by a later rebuild that has to walk. A file ignored only by
  the global git excludes file (`core.excludesFile`) is indexed as before (detect never read that file).
  An ignore file that is not UTF-8 (UTF-16 from Windows PowerShell 5.1, an ANSI code page) keeps the walk
  and its decoding. The detect() result has a new key
  `enumeration` (`"git"` or `"walk"`), and on the git path its `ignored` list is git's report of what
  .gitignore dropped.
- A full search-index build writes `.verinoda/index/search.db.build-<pid>` and renames it to `search.db`
  when complete; a build that fails keeps the previous index (it used to be deleted first). A leftover
  build file from a killed build is removed by the next full build after an hour, or can be deleted by hand.

## UPSTREAM.md note (docs/UPSTREAM.md, "Modified" table and the "No file ... is hand-edited" sentence)

`project_index/detect.py` is now hand-edited: `_git_env`, `_git_ls` (every git call, including the one
in `_git_tracked_path_keys`), `_git_rules`, `_git_rules_are_utf8`, `_git_listed_files`, `_git_kept_untracked`
(and its use in `ignored_predicate`), `_git_enumerate` and the `enumeration` parameter of `detect()`
(marked "Verinoda patch"; the Office-files branch was already a local edit). A
re-sync with `tools/port_upstream.py` overwrites the file: re-apply the patch and run
`tests/test_detect_git.py` and `tests_upstream/test_detect.py`.
