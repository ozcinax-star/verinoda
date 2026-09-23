"""MCP server for coding agents (``repoatlas mcp serve --repo X``).

The tools call the same core functions as the CLI (retrieval, index,
architecture_map, analysis, claims, critique, workflow, research, feedback),
so CLI and MCP answers never diverge. See :mod:`repoatlas.mcp.server`.

Naming note: this subpackage is ``repoatlas.mcp``. Imports are absolute, so
``import mcp`` / ``from mcp.server... import ...`` inside it still resolve to
the installed MCP SDK, not to this package.

Nothing here imports the MCP SDK at import time; the SDK is loaded only when a
server is built, so :class:`AtlasTools` works without it.
"""

from __future__ import annotations

__all__ = ["AtlasTools", "TOOL_NAMES", "build_server", "serve"]


def __getattr__(name: str):
    if name in __all__:
        from repoatlas.mcp import server

        return getattr(server, name)
    raise AttributeError(f"module 'repoatlas.mcp' has no attribute {name!r}")
