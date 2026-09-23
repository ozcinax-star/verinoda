"""RepoAtlas - evidence-first codebase analysis (derived from Graphify)."""

from __future__ import annotations

try:
    from importlib.metadata import version as _v

    __version__ = _v("repoatlas")
except Exception:  # pragma: no cover - running from a source tree
    __version__ = "0.1.0.dev0"
