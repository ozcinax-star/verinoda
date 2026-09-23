"""Where RepoAtlas keeps its state for a project.

Everything lives under ``<repo>/.repoatlas/``:

    atlas.db        claims, evidence, history, feedback, experiments (SQLite)
    index/          knowledge-graph output of project_index (graph.json, report)
                    plus RepoAtlas's derived, disposable files next to it:
                    search.db (passage index), receiver_calls.json (receiver-call
                    edges), lexicon.json (repo-learned vocabulary)
    runs/<id>/      raw experiment logs (never sent to a model wholesale)
    research/<slug> pinned checkouts of reference repositories
    config.json     budgets and experiment policy

The project_index package (derived from Graphify) reads its output directory
from the GRAPHIFY_OUT environment variable *at import time*, so the RepoAtlas
entry points call :func:`configure_index_env` before importing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ATLAS_DIRNAME = ".repoatlas"
INDEX_REL = f"{ATLAS_DIRNAME}/index"

DEFAULT_CONFIG: dict = {
    "budget": {"seconds": 60, "tool_calls": 40, "context_tokens": 6000},
    "experiments": {
        # Commands allowed to run with process-level isolation only.
        # Anything else needs container isolation (docker/podman) or is refused.
        "process_isolation_allowlist": [
            "pytest", "python -m pytest", "py -m pytest", "python3 -m pytest",
            "go test", "cargo test", "npm test", "node --test",
        ],
        "default_timeout": 120,
    },
}


def configure_index_env() -> None:
    """Point project_index at ``.repoatlas/index`` unless the user overrode it."""
    os.environ.setdefault("GRAPHIFY_OUT", INDEX_REL)


def _is_home_or_above(p: Path) -> bool:
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        return False
    return p == home or p in home.parents


def find_repo_root(start: Path | None = None) -> Path:
    """The project a command run from ``start`` is about.

    The *nearest* ancestor that is either a git work tree (``.git``) or an
    initialised RepoAtlas project (``.repoatlas/atlas.db``) wins; a bare
    ``.repoatlas`` directory without a database (for example one created by a
    tool install) does not count. The home directory and its ancestors are
    never chosen implicitly - only when the command is run from exactly there -
    so an unscanned project under ``~`` can never resolve to ``~`` itself.
    Falls back to ``start``.
    """
    start = (start or Path.cwd()).resolve()
    for p in (start, *start.parents):
        if p != start and _is_home_or_above(p):
            break
        if (p / ".git").exists() or (p / ATLAS_DIRNAME / "atlas.db").is_file():
            return p
    return start


def atlas_dir(repo: Path) -> Path:
    return Path(repo).resolve() / ATLAS_DIRNAME


def db_path(repo: Path) -> Path:
    return atlas_dir(repo) / "atlas.db"


def index_dir(repo: Path) -> Path:
    configure_index_env()
    out = os.environ["GRAPHIFY_OUT"]
    p = Path(out)
    return p if p.is_absolute() else Path(repo).resolve() / p


def graph_path(repo: Path) -> Path:
    return index_dir(repo) / "graph.json"


def search_db_path(repo: Path) -> Path:
    """The passage-level lexical index (disposable; rebuilt from the graph and files)."""
    return index_dir(repo) / "search.db"


def receiver_calls_path(repo: Path) -> Path:
    """Receiver-call edges computed at scan/update time, keyed by the graph file."""
    return index_dir(repo) / "receiver_calls.json"


def lexicon_path(repo: Path) -> Path:
    """Repo-learned lexicon (docs/DESIGN.md D6)."""
    return index_dir(repo) / "lexicon.json"


def runs_dir(repo: Path) -> Path:
    return atlas_dir(repo) / "runs"


def research_dir(repo: Path) -> Path:
    return atlas_dir(repo) / "research"


def ensure_atlas(repo: Path) -> Path:
    d = atlas_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    # Keep RepoAtlas state out of the user's git without touching their .gitignore.
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    return d


def load_config(repo: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = atlas_dir(repo) / "config.json"
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cfg
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg
