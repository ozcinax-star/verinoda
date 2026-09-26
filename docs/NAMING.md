# Product name

The product is called **Verinoda**. It was developed under the working name
"RepoAtlas" until 2026-09-23. That name was dropped because `repoatlas` and
`repoatlas-cli` were already taken on PyPI by projects in the same category
(repository maps and codebase Q&A for AI agents).

## Availability of "verinoda"

Checked on 2026-09-23:

| Namespace | Result |
|---|---|
| PyPI (`/pypi/verinoda/json`, `/simple/verinoda/`) | free (404) |
| npm registry | free (404) |
| GitHub repositories named "verinoda" | 0 |
| GitHub users/orgs "verinoda" | 0 |

Published on 2026-09-26: 0.1.0 on PyPI (https://pypi.org/project/verinoda/) and npm
(https://www.npmjs.com/package/verinoda), so both names now belong to this project.
| `verinoda.com` | does not resolve |

No trademark search was done. Do one in the target markets before a public
package release.

## Where the name appears

| Place | Value |
|---|---|
| Distribution and console script (`pyproject.toml`) | `verinoda` |
| Python import package | `verinoda` (vendored Graphify code: `verinoda.project_index`) |
| Per-project state directory | `.verinoda/` |
| Environment variables | `VERINODA_*` |
| Claude Code skill | `.claude/skills/verinoda/SKILL.md`, invoked as `/verinoda` |
| Codex skill | `.agents/skills/verinoda/SKILL.md`, invoked with `$verinoda` |
| MCP server key | `verinoda` |
| Installer ownership marker | `<!-- verinoda-managed v1 -->` |

Graphify's names (`graphify`, `graphifyy`) and its logo and wording are not
used for Verinoda's CLI, skills, docs or visuals, except where the upstream
origin is described.

## Historical names kept on purpose

- `benchmarks/results/**` was produced before the rename. Approach ids there
  read `repoatlas_*`; they correspond to `verinoda_*` today.
- The `heldout_repoatlas` question set and its corpus snapshot
  (`benchmarks/corpora/heldout_repoatlas_7371990/`) are the source of the
  product at commit 7371990 of the pre-rename history, when the package was
  still called `repoatlas`. Its gold locators name those old paths.

## Migrating a project from the working name

Projects indexed before the rename have a `.repoatlas/` directory. Verinoda
does not read it. Run `verinoda scan .` to build `.verinoda/`, then delete
`.repoatlas/` once you no longer need its claim history. Skills installed as
`repoatlas` must be removed with the old tool (`repoatlas uninstall ...`) or
by hand, then reinstalled with `verinoda install ...`.
