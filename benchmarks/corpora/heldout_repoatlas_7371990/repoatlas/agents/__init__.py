"""Claude Code and Codex integration: install/uninstall the RepoAtlas skill and MCP server.

Public API (used by ``repoatlas install``/``uninstall`` and ``repoatlas doctor``)::

    install(agent, scope, *, project_dir, home=None, with_mcp=True, dry_run=False) -> dict
    uninstall(agent, scope, *, project_dir, home=None, dry_run=False) -> dict
    status(project_dir, home=None) -> dict   # {"claude:project": {...}, ...}
    render(res) -> None

``home=None`` means :func:`pathlib.Path.home`. See :mod:`repoatlas.agents.installer`
for the locations and the ownership/manifest rules.
"""

from __future__ import annotations

from repoatlas.agents.installer import (
    AGENTS,
    MARKER,
    SCOPES,
    install,
    render,
    render_skill,
    server_command,
    status,
    uninstall,
)

__all__ = ["AGENTS", "MARKER", "SCOPES", "install", "render", "render_skill", "server_command", "status",
           "uninstall"]
