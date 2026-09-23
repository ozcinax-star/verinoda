"""Claude Code and Codex integration: install/uninstall the Verinoda skill and MCP server.

Public API (used by ``verinoda install``/``uninstall`` and ``verinoda doctor``)::

    install(agent, scope, *, project_dir, home=None, with_mcp=True, dry_run=False) -> dict
    uninstall(agent, scope, *, project_dir, home=None, dry_run=False) -> dict
    status(project_dir, home=None) -> dict   # {"claude:project": {...}, ...}
    render(res) -> None

``home=None`` means :func:`pathlib.Path.home`. See :mod:`verinoda.agents.installer`
for the locations and the ownership/manifest rules.
"""

from __future__ import annotations

from verinoda.agents.installer import (
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
