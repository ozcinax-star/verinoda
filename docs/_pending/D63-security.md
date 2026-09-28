# D63 (pending merge): running a project's own tests safely

Branch of the security review of `experiment run`, `observe`, `analyze --run-tests` and `review --run-tests`
(MCP `experiment_run`, `runtime_observe`, `analyze(run_tests/observe)`, `change_review(run_tests/observe)`,
`change_probe`, the debug ledger). To be merged by the operator into docs/DESIGN.md (a new section, the decision
table row below), docs/UPGRADING.md (the note below) and, where they describe the old behaviour, README.md and
docs/ARCHITECTURE.md (see "Docs that describe the old behaviour" at the end).

The doc edits made on the branch: README.md's Commands table has a row for the new `trust` command
(tests/test_docs.py requires every CLI command there), and the agent skill templates
(`verinoda/agents/templates/claude_SKILL.md`, `codex_SKILL.md`) say where `mcp.profile` now goes and that the
agent never runs `verinoda trust` (review fixes, see "Review fixes" at the end).

## DESIGN section: 36. Running a project's own tests safely (D63, 2026-09-28)

### 36.1 Why

A security review of the test-running paths found that process isolation was honest about its limits (the
results said the tests "can read and write files outside the copy") but that the *default* was to run any
repository's tests that way, including a freshly cloned one, and that several inputs let a repository or a
steered agent get past the argument policy:

- a repository's own `.verinoda/config.json` (force-added past `.verinoda/.gitignore`, so it arrives with a
  clone) could widen `experiments.process_isolation_allowlist` (to `python`, so `python -c ...` passed), pick
  the container image, and switch the MCP server to the full tool list;
- `python_for()` started `<repo>/.venv/Scripts/python.exe` of the original tree without any check: in a clone
  that file can be any program;
- argv[0] was checked by its basename only (`C:/anywhere/pytest.exe` passed), and a bare runner name was found
  with `shutil.which`, which on Windows looks in the current directory first (a `pytest.cmd` at the root of the
  project Verinoda runs from would start);
- pytest takes arguments the policy never saw: `@file` (pytest's parser has `fromfile_prefix_chars="@"`),
  `-o addopts=...` (nested options were not path-checked), `-p NAME` (any importable module), and the
  `addopts` and path settings of `pytest.ini` / `.pytest.ini` / `pytest.toml` / `pyproject.toml` / `tox.ini`
  / `setup.cfg` in the copy;
- the working-tree copy followed symbolic links and junctions, so a tracked link pulled an outside file's
  content into the copy and the tree hash;
- the container path ran as root, with a writable root file system and all default capabilities, gave the
  docker client a scrubbed environment (contexts, rootless sockets and `~/.docker` were lost), and had never run
  for real in the suite or CI.

For an untrusted repository no argument rule is a boundary: its tests, its `conftest.py`, `npm test`'s script,
`build.rs` are its own code and run with the user's privileges. So the decision is who trusts the code, and
the argument rules protect a trusted repository from an agent whose arguments can be steered.

### 36.2 Decisions

- **Trust is the user's decision, stored outside every repository.** `verinoda trust [path] [--subfolders] [--yes]
  [--remove] [--list]` writes `trust.json` in the per-user directory (`$VERINODA_CONFIG_DIR`, else
  `%APPDATA%\verinoda` on Windows, `$XDG_CONFIG_HOME/verinoda` or `~/.config/verinoda` elsewhere; keyed by the
  resolved path). `--subfolders` covers the folders below, never those under a `.verinoda` folder (reference
  checkouts live there), and is refused for the home folder and a drive root. No MCP tool sets trust: an agent
  steered by the repository's text cannot trust it through MCP. An agent with a shell can run the command, so
  trusting asks the user to confirm on a terminal and, without a terminal, is refused unless `--yes` is given
  (a speed bump with a clear message, not a boundary: an agent that adds `--yes` gets past it); the skill
  templates tell the agent never to run it. `--remove` works on a folder deleted since (else the stale entry
  would trust whatever is created there later) and `--list` marks such entries `missing`. A relative
  `$VERINODA_CONFIG_DIR` is ignored (it would resolve against the current directory, which can be a clone that
  ships its own `trust.json`); `~` is expanded.
- **An untrusted project's tests run only in a container.** `experiments.run` chooses process isolation only for
  an allowlisted command in a trusted project; an untrusted one goes to docker/podman when available, else it
  is refused with a `next_step` addressed to the user through the agent: "ask the user: if they trust this
  project's code, they run `verinoda trust <path>` themselves in a terminal ... an agent must never run it for
  them; or install docker/podman". Every entry point goes through `experiments.run`, so this covers `experiment run`,
  `observe`, `analyze --run-tests`, `review --run-tests`, `verify --run`, `probe`, the debug ledger and their MCP
  tools; each passes the refusal's `next_step` on (`ExperimentRefused.next_step`). Results carry `trusted`.
- **Protected settings.** `experiments.*`, `mcp.profile` and `research.network` come from the defaults, the
  user-level `config.json` in the same per-user directory, and a project's own `.verinoda/config.json` only
  when the project is trusted (`paths.PROTECTED_SETTINGS`, `load_config`, `resolve_profile`). The other settings
  still come from the project's file. What was ignored is said: in the experiment's `limits` and refusal
  (`ignored_settings_note`), and on stderr when `verinoda mcp serve` starts. Values equal to the ones in use
  (what `verinoda init` writes) are not reported. `resolve_profile` does not read an untrusted project's file at
  all, so a broken one cannot stop `mcp serve`.
- **argv[0].** A bare runner name is looked up in the absolute PATH directories only (`experiments._which`, with
  PATHEXT on Windows); when it is not there the run is an error, never a bare name handed to the OS. A path is
  accepted only for a Python interpreter this system knows (`codecheck_env.known_interpreter`: Verinoda's own,
  the registry, PATH, a Python manager's directory), a virtual environment outside the project made from one,
  or the trusted project's own `.venv` (`python_for` returns it only for a trusted project). Any other runner
  must be a bare name. The container runtime is resolved the same way.
- **pytest.** Refused under process isolation: `@file` arguments, `-p NAME` other than `-p no:NAME` and
  Verinoda's own plugins of that run (read the way pytest's `consider_preparse` reads it: `-p X` and `-pX`
  anywhere, after a `--` too; `-qpX` loads nothing and stays allowed), `-o addopts=<something>` (`-o addopts=`,
  which switches the files' addopts off, stays allowed; probe uses it). Single-dash clusters are split the way
  pytest's argparse reads them before `-o` and `-c` are looked for and before the path check (`-qoaddopts=X` is
  `-q -oaddopts=X`, `-qcFILE` is `-q -cFILE`, `-q=oX` is `-q -oX`; every letter other than pytest's value-taking
  `k m W c p o r` counts as a flag, so a plugin's flag cannot hide one). A path that names an environment
  variable (`%NAME%`, `$NAME`, `${NAME}`) is refused like an absolute one: pytest expands them in `--junitxml`,
  `--rootdir` and `cache_dir`, and the child's environment has `SYSTEMDRIVE`, `WINDIR`, `LANG`, `PATH`, `HOME`.
  Path candidates are found recursively (`-o addopts=--junitxml=/x`,
  `--override-ini=addopts=--basetemp=/x`, several words in one ini value). After the copy is made, the config
  files pytest may read (the copy's root and every folder down to each path argument, and a `-c` file) are
  parsed: `addopts` gets the same check as the arguments (unless `-o addopts=`), except that `-p NAME` there is
  refused only in a file the command names with `-c` and pytest would not find itself (any file of the copy, a
  test fixture too): in the config pytest finds, it is the trusted project's own choice, like `pytest_plugins`
  in its `conftest.py` (`addopts = -p pytester` is common). `cache_dir`, `log_file`,
  `pythonpath`, `testpaths` and `pytester_example_dir` must stay inside the copy, each resolved where pytest
  resolves it (`log_file` from the working directory, the others from the file's folder). A string value is
  split like pytest splits it and, for the single-string settings (`cache_dir`, `log_file`), also checked
  whole; a TOML list item is taken as it is, as pytest does (a shell split would eat a Windows `..\`). Nothing
  is rewritten: a refusal names the file and the setting, and its `next_step` says what to change for that rule
  (a path, the `-p` of a `-c` file, the file's syntax). TOML is read with tomllib, else tomli, else a
  conservative reader (every assignment of those keys, as the strings in its value, each whole and split). The run gets
  `-p no:cacheprovider` (or, when `--lf`/`--ff`/`--sw`... need the cache, `-o cache_dir=` in the throw-away
  folder) and `--basetemp` in the throw-away folder, before a `--`, so they come after the files' addopts; the
  record keeps the caller's command in `command` and the command run in `environment.argv`.
  `analyze --run-tests` now passes `-p no:cacheprovider` like review and observe.
- **The copy follows no link.** A working-tree file that is, or lies under, a symbolic link or a junction is not
  copied and is listed in `source.skipped` (with a limit line), as the commit copy already did. Read from one
  directory listing per folder; on Windows only symlink and mount-point reparse tags count (cloud placeholder
  files are ordinary files). Best effort: a folder whose listing cannot be read is not checked.
- **`verify --run`** in a project whose re-run is refused verifies as without `--run` and says so (`run.refused`,
  `run.next_step`); a run that did not happen neither confirms nor refutes the claim.
- **Container hardening.** `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges`, the host
  user (`--user uid:gid`; rootless podman `--userns=keep-id`, because `--user` there is a sub-uid the user
  cannot delete; Windows `--user 1000:1000`), `HOME=/tmp`, `PYTHONDONTWRITEBYTECODE=1`. The docker/podman client
  gets the real environment's daemon settings (`DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CONFIG`,
  `CONTAINER_HOST`, `XDG_RUNTIME_DIR`, the real HOME, ...; `CLIENT_ENV_ALLOW`), never the secrets the scrubbed
  environment drops; the container gets only the `-e` values. The image is `experiments.container_image`
  (user config or a trusted project), default `python:3.12-slim`, which has no pytest: such a run is
  `inconclusive` with a `next_step` naming the setting and the user config file.
- **Tests and CI.** The test suite points `VERINODA_CONFIG_DIR` at a temporary folder and trusts its temporary
  folder with subfolders (tests/conftest.py), the way a user would; tests of the untrusted path use a fresh
  per-user folder. `tests/test_experiments_container.py` runs the container path for real and skips unless
  `VERINODA_CONTAINER_TESTS=1`; the CI job `container` (ubuntu-latest, real docker, an image with pytest built
  in the job) sets it, and there a missing runtime fails instead of skipping.

What remains (not done here, R8/R9 of the review): **OS-level confinement without Docker.** An untrusted project
on a machine without docker/podman is refused, not confined. Candidates, each to report what it covered in
`guarantees` and to fall back to "refuse", never to unconfined: Linux bubblewrap (`bwrap --unshare-all`, binds
of the interpreter and the copy) or Landlock; macOS `sandbox-exec` with a deny-default profile; Windows a
low-integrity token with a low-integrity copy folder (writes), AppContainer (network and reads). Also open:
Windows job-object memory and process-count limits (`resource_limits` is still false on Windows under process
isolation), and trust keyed by the remote URL as well as the path.

### 36.3 Measured

Decision functions called directly, before and after (Windows, Python 3.12, pytest 9.1.1; no child process):

| Input | Before | After |
|---|---|---|
| `python -m pytest @args.txt` | allowlisted | risky (@file) |
| `python -m pytest -p someplugin` | allowlisted | risky (-p) |
| `C:/anywhere/pytest.exe -q`, `C:/anywhere/npm.exe test` | allowlisted | risky (argv[0] by path) |
| `python -m pytest -o addopts=--junitxml=C:/x.xml` (and `-oaddopts=`, `--override-ini=addopts=--basetemp=C:/x`) | allowlisted | risky (absolute path) |
| `python -m pytest --junitxml=C:/x.xml` | risky | risky |
| `npm test` | allowlisted | allowlisted (runs only in a trusted project or a container) |
| repo config `{"experiments": {"process_isolation_allowlist": ["python"]}, "mcp": {"profile": "full"}, "research": {"network": "on"}}`, project not trusted | allowlist `['python']`, profile full, network on | defaults, profile core, network cache (reported as ignored) |
| `python_for` with `<repo>/.venv/Scripts/python.exe` present, project not trusted | the repository's file | Verinoda's own interpreter |
| `zzvnprobe.cmd` in the current directory, not on PATH | `shutil.which`: `.\zzvnprobe.CMD` | `_which`: not found |
| `python -m pytest --junitxml=%SYSTEMDRIVE%/Users/Public/x.xml` (also `--rootdir=%SYSTEMDRIVE%/`) | allowlisted (the review wrote a file outside the copy through `%LANG%`) | risky (environment variable) |
| `python -m pytest tests -- -pX`, `-qoaddopts=-pX`, `-qo addopts=-pX` | allowlisted (the review's runs imported X) | risky (-p / addopts override) |
| `python -m pytest -qcsub/evil.ini` with `addopts = -pX` in that file | ran (X imported) | refused (the `-c` file's addopts) |
| trusted project, `pytest.ini` `addopts = -p pytester` | refused without a container (a regression of this branch) | runs with process isolation |
| `pyproject.toml` `addopts = ["--junitxml=..\\..\\out\\x.xml"]` | allowed (shell split ate the backslashes; the review wrote the file outside) | refused ('..') |

- Link check of the copy: 121-170 ms for 2,561 files in 335 folders (this repository, Windows, one directory
  listing per folder, five runs); the copy itself took 7-43 s on the same (shared, loaded) machine, so the check
  is a few percent at most.
- Tests: tests/test_experiments_trust.py (84 tests: protected settings, trust store and command (confirmation,
  `--yes`, a deleted folder), refusal and next_step per entry point (verify --run included), container command
  and client environment through a fake runtime, argv[0], `_which` without the current directory, the pytest
  argument and config-file rules (environment variables, `-p` after `--`, clusters, `-p` in a `-c` file only, TOML
  list items), a real pytest run showing it reads those spellings, the TOML fallback, `--basetemp` in the
  throw-away folder, links not followed, a relative `VERINODA_CONFIG_DIR`, an untrusted project's broken
  config and `mcp serve`); tests/test_experiments.py updated for the argv[0] rule; tests/test_agents.py checks
  the templates' trust and profile lines.
- Not measured here: the container path itself (no docker/podman on this machine). The CI job `container` is its
  first real run; the flags follow the docker and podman documentation.

## DESIGN decision table row

| D63 | Running a project's own tests safely | implemented | Built 2026-09-28 (section 36): `verinoda trust` records trusted projects outside every repository; an untrusted project's tests run only in a container, else they are refused with a next_step; `experiments.*`, `mcp.profile` and `research.network` come from a project's own config only when it is trusted; argv[0] must be a known interpreter or a bare name found on PATH without the current directory; pytest `@file`, `-p NAME`, `-o addopts=...` and the config files' addopts and path settings are checked; the copy follows no link; the container runs read-only, without capabilities, as the host user. OS confinement without Docker is not done. |

## UPGRADING note

Add under a new heading "Upgrading to the 2026-09-28 code (D63)":

- **Your projects are not trusted until you say so.** `analyze --run-tests`, `review --run-tests`,
  `observe`, `experiment run`, `verify --run`, `probe` and the debug ledger - and the MCP tools behind them,
  including core `change_review(run_tests/observe)` - now refuse to run an untrusted project's tests with
  process isolation (the result says `refused ... the project is not trusted` with the next step). Run
  `verinoda trust <path>` once per project whose code you trust (or `verinoda trust <folder> --subfolders` for a
  folder of your own projects); it asks you to confirm, and in a script without a terminal needs `--yes`. Do
  not let an agent run it for you. With docker or podman installed, an untrusted project's tests run in a
  container instead. `verinoda trust --list` shows the list (a folder that no longer exists is marked
  `missing`), `--remove` takes one off. The record lives in `%APPDATA%\verinoda\trust.json` (Windows) or
  `~/.config/verinoda/trust.json`; `VERINODA_CONFIG_DIR` (an absolute path; a relative one is ignored) moves it.
- **Settings that moved.** `experiments.*` (allowlist, container image, default timeout), `mcp.profile` and
  `research.network` in a project's `.verinoda/config.json` apply only when the project is trusted. Otherwise
  they are ignored and the experiment result (and `verinoda mcp serve` on stderr) says so. To keep one for an
  untrusted project, put it in the user-level `config.json` next to `trust.json` (it applies to every project),
  or trust the project. `"mcp": {"profile": "full"}` in the project's file (README, `mcp serve` row) therefore
  needs a trusted project, `--profile full`, or the user-level config. In a trusted project the project's file
  still wins over the user-level one, and `verinoda init` writes every default into it (`"mcp": {"profile":
  "core"}`, `"research": {"network": "cache"}`, the experiment settings): delete a key from the project's file
  to use the user-level value.
- **An interpreter given by path** (`experiment run -- C:/.../python.exe -m pytest`) must be one this system
  knows (Verinoda's own, the registry, PATH, a Python manager's folder), a virtual environment made from one
  outside the project, or the trusted project's own `.venv`. Other runners (`pytest`, `npm`, `go`, `cargo`,
  `node`) must be given as bare names; they are looked up on PATH but never in the current directory, and a name
  not on PATH is an error.
- **pytest runs** get `-p no:cacheprovider` and a `--basetemp` in the throw-away folder added (`--lf` and friends
  keep working, with an empty cache). Refused under process isolation: `@file` arguments, `-p NAME` (except
  `-p no:NAME`; after a `--` too), `-o addopts=...` (also as `-qoaddopts=...`), a path that names an environment
  variable (`--junitxml=%TEMP%/x.xml`), and an `addopts` or path setting (`cache_dir`, `log_file`, `pythonpath`,
  `testpaths`) in the project's pytest config files that points outside the project: the refusal names the file
  and the setting. A `-p` in the `addopts` of the project's own config (`addopts = -p pytester`) runs; one in a
  file named with `-c` that pytest would not find itself is refused.
- **Symbolic links and junctions** in the working tree are not copied into the throw-away copy (listed under
  `source.skipped`); a test that reads a fixture through a link sees it missing.
- **Containers** run with `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges` as your
  user (`--userns=keep-id` for podman). A test that writes outside `/work` and `/tmp` fails there. Set
  `experiments.container_image` in the user-level config to an image with pytest and the project's
  dependencies; the default `python:3.12-slim` has no pytest and gives `inconclusive`.
- Scripts that call `experiments.policy()` directly: an absolute interpreter path that does not exist or is not
  known is now `risky` (pass `repo=` for a trusted project's `.venv`, `plugins=` for your own `-p` modules).

## Docs that describe the old behaviour (for the operator's merge)

- README.md, Commands table, `mcp serve` row: "`\"mcp\": {\"profile\": \"full\"}` in `.verinoda/config.json`,
  serves all" - add "in a trusted project (`verinoda trust`) or in the user-level config".
- README.md / docs/UPGRADING.md `.verinoda/config.json` row: the user config is merged over the defaults; add
  the user-level config and the protected settings.
- docs/ARCHITECTURE.md, "State on disk": add the per-user directory (`config.json`, `trust.json`) outside the
  project.

## Review fixes (adversarial review of this branch, 2026-09-28)

- H1: a path argument or config value that names an environment variable was accepted and pytest expanded it
  (`--junitxml=%SYSTEMDRIVE%/...` overwrote a file outside the copy): refused now, in argv and in config files.
- M1: `-p` after `--`, `-o addopts=` inside a cluster (`-qoaddopts=`), and a config file named inside a cluster
  (`-qcFILE`) got past the rules: pytest's own reading is mirrored now (a test runs pytest to show it reads them).
- M2: this branch refused a trusted project whose own config says `addopts = -p pytester`: `-p` in addopts is now
  checked only in a file the command names with `-c`; the refusal's `next_step` is per rule.
- M3: `verinoda trust` confirms on a terminal and needs `--yes` without one; the refusal's `next_step` and the
  skill templates tell the agent the decision is the user's and never to run it.
- L1-L5: TOML list items taken as pytest takes them; `trust --remove` of a deleted folder; the skill templates'
  `mcp.profile` advice; an untrusted project's broken config no longer stops `mcp serve`; a relative
  `VERINODA_CONFIG_DIR` is ignored.
- Open (L6, not reproducible without a container runtime): podman detection by file name (`--userns=keep-id` for
  rootful podman, `--user uid:gid` for the podman-docker shim). The CI job `container` runs docker only.
