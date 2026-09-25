"""Where Verinoda keeps its state for a project.

Everything lives under ``<repo>/.verinoda/``:

    atlas.db        claims, evidence, history, feedback, experiments (SQLite)
    index/          knowledge-graph output of project_index (graph.json, report)
                    plus Verinoda's derived, disposable files next to it:
                    search.db (passage index), receiver_calls.json (receiver-call
                    edges), lexicon.json (repo-learned vocabulary), python_facts.json
                    (what each .py file imports and calls, kept between builds),
                    python_cross.json (the part of each .py file's syntax tree the
                    cross-file import pass walks, kept between builds),
                    rebuild_record.json (what the last full rebuild built when Verinoda
                    rewrote graph.json after it, so an update that builds the same graph
                    keeps graph.json), empty_json.json
                    (data-shaped .json files the extractor skips, by path and bytes) and,
                    when the user supplies one, index.scip (a SCIP index read by
                    scip_reader; freshness state in scip_fresh.json)
    plans/          question-plan files (JSON is never passed on the command line)
    decisions/      decision records of what the human chose (verinoda.decisions; ``decisions.dir``
                    in config.json moves them to a folder the project commits)
    runs/<id>/      raw experiment logs (never sent to a model wholesale); for a debug
                    attempt its change.patch vs the session base (and an agent-reported
                    run's output); runs/blobs/ (changed files by content id) and
                    runs/base-ids/ (a base commit's content ids) serve the debug ledger
    research/<slug> pinned checkouts of reference repositories
                    (research/http-cache: offline-first HTTP cache of the reference resolver)
    config.json     budgets, experiment policy, research network mode, understanding thresholds

The project_index package (derived from Graphify) reads its output directory
from the GRAPHIFY_OUT environment variable *at import time*, so the Verinoda
entry points call :func:`configure_index_env` before importing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ATLAS_DIRNAME = ".verinoda"
INDEX_REL = f"{ATLAS_DIRNAME}/index"

DEFAULT_CONFIG: dict = {
    # precise_*: fresh (uncached) precise call-site resolutions per analysis (docs/DESIGN.md D29;
    # verinoda.precise.Budget.from_config falls back to the same values).
    "budget": {"seconds": 60, "tool_calls": 40, "context_tokens": 6000,
               "precise_sites": 20, "precise_seconds": 1.5},
    "experiments": {
        # Commands allowed to run with process-level isolation only.
        # Anything else needs container isolation (docker/podman) or is refused.
        "process_isolation_allowlist": [
            "pytest", "python -m pytest", "py -m pytest", "python3 -m pytest",
            "go test", "cargo test", "npm test", "node --test",
        ],
        "default_timeout": 120,
    },
    # Reference resolution / research network mode: "off" (no network), "cache" (the offline-first
    # HTTP cache under research/http-cache, network only on a miss) or "on" (docs/DESIGN.md D15).
    "research": {"network": "cache"},
    # Mention-linking thresholds of the question-plan check (docs/DESIGN.md D3); the values are
    # verinoda.question_plan.THRESHOLDS (tests/test_cli.py keeps the two equal).
    "understanding": {"link": 0.70, "weak": 0.40, "margin": 0.15, "fuzzy": 90, "did_you_mean": 85,
                      "max_clarifications": 3},
    # Reference trees (an original implementation being ported, a vendored copy): paths, or
    # {"path": ..., "aliases": [...]}, whose code ranks lower unless the question names the path
    # or an alias (verinoda.search_index.REFERENCE_FACTOR). `verinoda setup --reference PATH` adds one.
    "index": {"reference": []},
    # Debug ledger (docs/DESIGN.md D34): stop after this many fix attempts in a row without measured progress;
    # reruns of the flaky-check strategy; bisect run budget.
    "debug": {"max_no_progress": 3, "rerun_times": 5, "bisect_max_runs": 12},
    # MCP tools served (`verinoda mcp serve --profile` wins): "core" (11 tools: query, analyze, inspect,
    # trace, map, claims and evidence, index_update, code_check, decision_check) or "full" (all 33)
    "mcp": {"profile": "core"},
}

NETWORK_MODES = ("off", "cache", "on")


def configure_index_env() -> None:
    """Point project_index at ``.verinoda/index`` unless the user overrode it."""
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
    initialised Verinoda project (``.verinoda/atlas.db``) wins; a bare
    ``.verinoda`` directory without a database (for example one created by a
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


def scip_index_path(repo: Path) -> Path:
    """Where ``verinoda scan --scip FILE`` puts a user-supplied SCIP index.

    Always ``.verinoda/index/index.scip`` (not :func:`index_dir`): it is one of
    the two places :func:`verinoda.scip_reader.find_index` looks.
    """
    return atlas_dir(repo) / "index" / "index.scip"


def plans_dir(repo: Path) -> Path:
    """Question-plan files (``verinoda plan draft`` writes here; docs/DESIGN.md D1)."""
    return atlas_dir(repo) / "plans"


def runs_dir(repo: Path) -> Path:
    return atlas_dir(repo) / "runs"


def research_dir(repo: Path) -> Path:
    return atlas_dir(repo) / "research"


def http_cache_dir(repo: Path) -> Path:
    """The reference resolver's HTTP cache (``hosts.json`` records rate-limit blocks)."""
    return research_dir(repo) / "http-cache"


def network_mode(repo: Path) -> str:
    """``research.network`` from the config, ``cache`` when it is missing or not a known mode."""
    mode = (load_config(repo).get("research") or {}).get("network")
    return mode if mode in NETWORK_MODES else "cache"


def ensure_atlas(repo: Path) -> Path:
    d = atlas_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    # Keep Verinoda state out of the user's git without touching their .gitignore.
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
