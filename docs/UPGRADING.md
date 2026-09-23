# Versioning and upgrades

Verinoda follows semantic versioning once it leaves `0.x`. While in `0.x`,
minor versions may change CLI output and MCP tool results. The SQLite schema is
always migrated forward, never silently reset.

## What changes on upgrade

| Component | Upgrade path |
|---|---|
| Package / CLI | Re-run the one-line installer from the README (`install.ps1` / `install.sh`); it reinstalls from the chosen ref (`VERINODA_REF`, default `main`) in copy mode. Otherwise `uv tool upgrade verinoda` · `pipx upgrade verinoda` · `pip install -U <wheel>`. The name is not on PyPI yet (see `NAMING.md`), so install from a wheel or a git path. For Codex on Windows, reinstall with `uv tool install --link-mode copy <wheel-or-path>` (see the README). |
| `.verinoda/atlas.db` | `verinoda.store` keeps `meta.schema_version`. Opening an older database applies the migrations in `_MIGRATIONS` in order (v1 → v2 → v3 → v4). A database newer than the installed Verinoda is refused with an explicit error instead of being modified. Claims, evidence and history are never deleted by a migration. |
| `.verinoda/index/` | Derived, disposable data (see below). After an upgrade that changes extraction, run `verinoda scan <repo> --force`. Claims whose dependencies changed are marked `stale` by the normal snapshot comparison, not by the upgrade itself. |
| `.verinoda/config.json` | Optional. A user config is merged key by key over `paths.DEFAULT_CONFIG`, so new keys (`budget.precise_sites`, `budget.precise_seconds`, `research.network`, `understanding.*`) take their defaults when absent. |
| Agent skills / MCP registrations | Re-run `verinoda setup` in the project (it also updates the index and installs for the agents found on PATH), or `verinoda install --agent <claude\|codex> --scope <project\|user>`. This brings the skills' "understand the question first" and "references the user gives" sections and the new allowed-tools entries (`verinoda plan *`, `verinoda resolve *`). Install is idempotent, rewrites only files carrying the Verinoda ownership marker, and records what it wrote in the install manifest; `verinoda uninstall` removes exactly those entries. The MCP tool list comes from the server itself, so the 23 tools appear without re-registration. |
| Upstream (Graphify) base | Maintainers only: `python tools/port_upstream.py <graphify-checkout-at-new-commit>`, review the diff, run `pytest tests` and `pytest tests_upstream`, update `docs/UPSTREAM.md` (commit, test table, inventory). Check that `index.install_path_identity_memo()` still finds `watch._StoredSourcePaths` (`tests/test_index.py` covers it). |

## Upgrading to the round-3 code (schema v3 / v4)

### Schema

- **v2** adds triggers so that claim-evidence links, snapshots, experiments,
  analyses and research records can no longer be deleted, and links can no
  longer be changed.
- **v3** adds one migration for all round-3 subsystems:
  - new tables: `question_plans`, `file_facts`, `claim_deps`,
    `evidence_locations`, `runtime_runs`, `runtime_calls`, `resolutions`,
    `reference_resolutions`, `file_stat`;
  - new columns: `feedback.plan_id`, `analyses.plan_id`, `claims.claim_key`,
    `claims.verified_at` and `claim_evidence.grp` (evidence groups).
- **v4** makes `claims.text` and `claims.created_at` immutable (trigger
  `no_update_claim_identity`). A correction must supersede a claim; it can no
  longer rewrite one in place. A script that updates `claims.text` directly
  now fails with "a claim's text and created_at are immutable; supersede it
  instead".

### Claims created before the upgrade

- They have no recorded dependencies (`claim_deps`), so invalidation uses the
  old file-level rule for them: any change to a cited or subject file makes
  them stale. Claims created after the upgrade get facet-level dependencies.
- Their evidence has no anchor. It is re-checked by exact hash, and a moved
  block is relocated only when it occurs once in the file (`ambiguous`
  otherwise). It is never relocated by guess.

### Status changes that are calibration, not regressions

The trust engine checks evidence more strictly than earlier versions. The same
claim can therefore get a *lower* status after an upgrade. This is intended:
the earlier status was not justified by its evidence.

- **A claim with zero evidence is now `unknown`** (it was `weak_inference`).
  For example, `verinoda claim add "..."` without `--source` gives
  `unknown`.
- **A verified status needs relevant evidence.** `*_verified` requires one
  evidence group that is verifying, fresh and graded `full` by `entail`: the
  evidence must mechanically state the claim, not merely exist. The check runs
  on every path that changes a status (`claims.check_status`). A claim that
  was `statically_verified` by term overlap can drop to `strong_inference` or
  lower on its next `verify` or `challenge`.
- **Search results, model summaries and user feedback are not support.** On
  their own they now give `weak_inference` at most; before, they were enough
  for `strong_inference`.
- **Only definitive refutations contradict.** A heuristic refutation now
  lowers a claim one step and adds an uncertainty instead of making it
  `contradicted`.
- **Critique is idempotent and never raises.** Re-running critique on an
  unchanged claim gives the same result. It no longer lifts a `stale` or
  `contradicted` claim back to `strong_inference`.
- **Foreign evidence is rejected.** `feedback resolve --verdict
  confirmed|qualified` accepts only evidence already linked to the claim or
  produced by that feedback's own protocol runs, and it never raises the
  claim.
- **`claim add --status stale` is no longer accepted.** Only `update` and
  `scan` set `stale`.

Status shares in older analyses and older benchmark result files are therefore
not directly comparable with new runs.

### Output and exit-code changes

- `verinoda query` prints the plain-text context by default. Use `--json`
  for the structured result. MCP `project_query` has `format="text"` by
  default.
- Every `analyze` runs through a question plan. The CLI exits with 2 for an
  invalid plan and 3 when the plan needs clarification. `verinoda resolve`,
  `observe` and `resolve-call` also use exit code 3 for "needs more".
- `verinoda index` blocks upstream installer, hook and `~/.graphify` commands
  (exit 2). See [UPSTREAM.md](UPSTREAM.md).

## Derived files are disposable

Everything under `.verinoda/index/` is derived from the repository and can be
deleted at any time. `verinoda scan <repo> --force` rebuilds it, and most of
it is also rebuilt automatically:

| File | Built by | Rebuilt automatically when |
|---|---|---|
| `graph.json`, `GRAPH_REPORT.md`, `graph.html`, `cache/` | `scan` / `update` (vendored Graphify pipeline) | the working tree changed (`update`, or `analyze` refreshing first) |
| `search.db` | `scan` / `update` (`search_index.update`) | its schema or tokenizer version differs (the next query rebuilds it) |
| `receiver_calls.json` | `scan` / `update` (receiver-call pass) | it does not match `graph.json` (it is recomputed) |
| `lexicon.json` | `scan` / `update` (`lexicon.build`, incremental by file hash) | the next `scan` / `update` |
| `scip_fresh.json` | the first read of a user-supplied `index.scip` | delete it together with `index.scip`, then `scan --scip FILE` again |

`index.scip` is the only file here that Verinoda cannot rebuild itself: it is
produced by the user's own SCIP indexer.

The caches inside `atlas.db` (`file_facts`, `resolutions`, `file_stat`) are
derived too. They are keyed by file content or stat, and they are recomputed
when they do not match. Everything else in `atlas.db` is audited history and is
never deleted.

## Optional extra: `verinoda[precise]`

Precise call-site resolution (`precise.py`, jedi) is optional. Without it,
every precise lookup answers "no precise answer" and relation claims that
depend on a method's receiver type stay `strong_inference`.

```bash
pip install "<wheel-or-path>[precise]"                                       # into a venv
uv tool install --link-mode copy --with "jedi>=0.19.2,<0.21" <wheel-or-path>  # uv tool
verinoda doctor          # "precise" check: available or why not
```

## Checking the state after an upgrade

`verinoda doctor` reports:

- the installed version and package layout (hardlinked or editable installs
  warn about agent sandboxes), and the upstream base commit;
- graph and snapshot freshness, the schema version and claim counts;
- the search index (schema/tokenizer version, units, stale files), whether
  `receiver_calls.json` matches `graph.json`, the lexicon and seed-dictionary
  size;
- whether precise resolution is available, whether the project's interpreter
  has `sys.monitoring` (without it the tracer falls back to `setprofile`, which
  is much slower), and the SCIP index and its share of fresh documents;
- the reference network mode, the HTTP cache size, rate-limited hosts and
  whether `packaging` is importable;
- agent skill and MCP installation state, and missing optional dependencies.
