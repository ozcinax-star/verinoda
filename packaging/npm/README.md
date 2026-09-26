# verinoda (npm)

> **Work in progress, no guarantees.** Verinoda is unfinished; see the main repository.

Evidence-first codebase analysis for coding agents. Verinoda itself is a Python program
([PyPI: verinoda](https://pypi.org/project/verinoda/)); this package only starts the PyPI release of the
same version, so it can be used from Node tooling and MCP configurations:

```bash
npx -y verinoda --version
npx -y verinoda setup            # inside a project: index it and connect Claude Code / Codex
npx -y verinoda query "where is the order total computed"
```

MCP (stdio), for clients that start servers with `npx`:

```json
{ "mcpServers": { "verinoda": { "command": "npx", "args": ["-y", "verinoda", "mcp", "serve"] } } }
```

How it runs the Python package, in order: `uvx` (recommended: it also fetches a suitable Python),
`pipx run`, or a private virtual environment made once per version with a Python 3.10+ found on PATH
(kept in the user's cache folder). `VERINODA_NPM_RUNNER=uvx|pipx|venv` forces one of them.

Installing Verinoda directly is simpler when you do not need npm: `uv tool install "verinoda[precise]"`
or `pipx install "verinoda[precise]"`. Documentation: https://github.com/ozcinax-star/verinoda
