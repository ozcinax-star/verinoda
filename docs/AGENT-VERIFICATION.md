# Agent integration: what was verified with the real tools

Date: 2026-09-22. Machine: Windows 11, Python 3.12.0. Claude Code 2.1.280,
codex-cli 0.154.0. Every run used a throw-away git-committed copy of
`examples/orders_app`. Verinoda was installed at **project scope**
(`verinoda install --agent claude|codex --scope project`). The user's real
home directory and global agent settings were not modified. User-scope installs
are covered by the automated tests (`tests/test_agents.py`), which run against a
temporary home.

## Claude Code — `/verinoda`

Command:

```
claude -p "/verinoda Where is the discount threshold configured and which function applies it?" \
       --output-format stream-json --verbose --max-turns 12
```

Observed:

- The session's init event listed `verinoda` among the available skills.
- The `/verinoda` prompt loaded the skill.
- Claude called the Verinoda MCP tools from the project `.mcp.json`
  (`mcp__verinoda__analyze`, `mcp__verinoda__project_query`) and the CLI.
- The run finished in 5 turns with a correct answer: `DISCOUNT_THRESHOLD` at
  `orders/config.py:7` (env `ORDERS_DISCOUNT_THRESHOLD`, default `100.0`), applied by
  `apply_discount()` at `orders/pricing.py:11-15`.
- Each claim was listed with its status and id; for example
  `[statically_verified 0.9] … (clm_535945050556, challenged: passed)`.

## Codex — `$verinoda`

Command:

```
codex exec --sandbox workspace-write --json '$verinoda <question>'
```

Observed:

- Codex read the skill from `.agents/skills/verinoda/SKILL.md` and followed its
  evidence protocol.
- It used the Verinoda MCP tools configured in `.codex/config.toml`: `analyze`,
  `project_query`, `claim_inspect`, `claim_challenge`.
- It answered two questions correctly, with status-labelled claims and `file:line`
  citations:
  - where the discount threshold is configured;
  - which function persists an order and how the handler reaches it. Here Codex
    kept `place_order() → repo.save()` at `strong_inference` and explained that the
    receiver type is inferred.
- Codex has no `/verinoda` command. The skill is invoked with a `$verinoda`
  mention, as the installer and skill text state.

### Windows sandbox finding and fix

Inside Codex's `workspace-write` sandbox on Windows, the `verinoda` **CLI** failed
to import its own package, while the **MCP** tools worked, because Codex starts MCP
servers outside the sandbox.

| Install of the CLI | In-sandbox result |
|---|---|
| editable dev install (`uv pip install -e .`) | `ModuleNotFoundError: No module named 'verinoda'` |
| `uv tool install <wheel>` (uv default: hardlinks from its cache), outside the workspace | `PermissionError: [Errno 13] … verinoda\doctor.py` |
| same, installed inside the workspace | same `PermissionError` |
| `UV_LINK_MODE=copy uv tool install <wheel>`, inside the workspace | `verinoda --version` and `verinoda doctor` exit 0 |
| `UV_LINK_MODE=copy uv tool install <wheel>`, outside the workspace | `verinoda --version` and `verinoda doctor` exit 0 |

The installed package files have a link count of 2 with uv's default mode and 1
with copy mode. As a result:

- `verinoda doctor` reports a `sandbox_readable` warning for hardlinked or
  editable installs;
- the Codex skill tells the agent to fall back to the MCP tools and to point the
  user at the fix;
- the recommended install for Codex on Windows is
  `uv tool install --link-mode copy <wheel-or-path>`.

Why the sandbox cannot read hardlinked files was not investigated further (most
likely the ACLs of uv's cache directory).

## Not verified here

- A human-driven, interactive Claude Code or Codex session (the runs above are headless).
- The same agent checks on Linux or macOS.
- User-scope installs against a real home directory. These were tested only with
  a temporary home.
