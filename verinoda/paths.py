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
                    (experiment policy, MCP profile and network mode only when the user trusts the
                    project; otherwise from the user-level config, see :func:`user_config_dir`)

Outside every project, in :func:`user_config_dir`: the user-level ``config.json`` and ``trust.json``
(the projects the user trusts, written by ``verinoda trust``).

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
    # vendored: keep the calls of vendored (vendor/, third_party/ ...), minified and generated files in the graph;
    # by default no call from them is extracted and a minified file is its file node only
    # (verinoda.project_index.vendored.vendored_reason).
    "index": {"reference": [], "vendored": False},
    # Debug ledger (docs/DESIGN.md D34): stop after this many fix attempts in a row without measured progress;
    # reruns of the flaky-check strategy; bisect run budget.
    "debug": {"max_no_progress": 3, "rerun_times": 5, "bisect_max_runs": 12},
    # MCP tools served (`verinoda mcp serve --profile` wins): "core" (11 tools: query, analyze, inspect,
    # trace, map, claims and evidence, index_update, code_check, decision_check) or "full" (all 33)
    "mcp": {"profile": "core"},
    # `verinoda query` / MCP project_query / analyze passages: with shape_budget a single-clause question
    # gets 4800 characters instead of 6000 (verinoda.retrieval.question_chars; env VERINODA_SHAPE_BUDGET)
    "query": {"shape_budget": False},
    # `verinoda update` also re-verifies stale claims and lists duplicate claims (verinoda.consolidate), as
    # `update --consolidate` does; so the git hooks' background updates do it too (`ui --watch` does not)
    "claims": {"consolidate_on_update": False},
    # `scan` / `update` recompute the stale named facts (verinoda.facts) within 10 s: searches re-run, input
    # statuses re-read, never `verify`
    "facts": {"refresh_on_update": True},
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


def index_vendored(repo: Path) -> bool:
    """Config ``index.vendored``: keep vendored, minified and generated code in the graph. On only for a JSON
    ``true`` (or the text "true", "1", "yes", "on"); ``"false"`` and anything else are off."""
    value = (load_config(repo).get("index") or {}).get("vendored")
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on")


# -- the user's own settings and trust, kept outside every repository (docs/DESIGN.md D63) --------------------

CONFIG_DIR_ENV = "VERINODA_CONFIG_DIR"
# Settings a repository's own .verinoda/config.json may set only when the user trusts that repository: what
# may run and how (experiments.*: the process-isolation allowlist, the container image, timeouts), which MCP
# tools are served (mcp.profile) and whether reference resolution goes to the network (research.network).
# A cloned repository can ship that file (force-added past .verinoda/.gitignore); without trust it cannot
# widen them. None: every key of the section.
PROTECTED_SETTINGS: dict[str, tuple[str, ...] | None] = {
    "experiments": None, "mcp": ("profile",), "research": ("network",)}


def user_config_dir() -> Path:
    """Verinoda's per-user directory (``$VERINODA_CONFIG_DIR``; else ``%APPDATA%\\verinoda`` on Windows,
    ``$XDG_CONFIG_HOME/verinoda`` or ``~/.config/verinoda`` elsewhere). No repository writes there.

    A relative ``$VERINODA_CONFIG_DIR`` is ignored: it would resolve against the current directory, which can
    be a cloned repository shipping its own ``trust.json`` and ``config.json``."""
    env = os.environ.get(CONFIG_DIR_ENV)
    if env and Path(env).expanduser().is_absolute():
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "verinoda"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "verinoda"


def user_config_path() -> Path:
    """The user-level config.json: every setting, the protected ones included, for every project."""
    return user_config_dir() / "config.json"


def trust_path() -> Path:
    """Where ``verinoda trust`` records the repositories the user trusts."""
    return user_config_dir() / "trust.json"


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _trust_key(p: Path | str) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def trust_entries() -> list[dict]:
    """The recorded trust entries (``path``, ``subfolders``, ``at``); unreadable entries are left out."""
    data = _read_json(trust_path())
    rows = data.get("trusted") if isinstance(data, dict) else None
    return [e for e in rows or [] if isinstance(e, dict) and isinstance(e.get("path"), str) and e["path"]]


def trust_record(repo: Path | str) -> dict | None:
    """The entry that makes ``repo`` trusted, or None. An entry covers its own path, and with ``subfolders``
    every folder below it except those under a ``.verinoda`` folder (reference checkouts live there)."""
    key = _trust_key(repo)
    for e in trust_entries():
        root = _trust_key(e["path"])
        if key == root:
            return e
        if e.get("subfolders") and key.startswith(root.rstrip(os.sep) + os.sep):
            if ATLAS_DIRNAME not in Path(key[len(root.rstrip(os.sep)) + 1:]).parts:
                return e
    return None


def is_trusted(repo: Path | str) -> bool:
    """Has the user marked ``repo`` trusted (``verinoda trust``)? Only then do its tests run with process
    isolation, and only then does its own config set the protected settings (:data:`PROTECTED_SETTINGS`)."""
    return trust_record(repo) is not None


def set_trust(path: Path | str, *, subfolders: bool = False, remove: bool = False) -> dict:
    """Record (or with ``remove`` drop) trust in ``path``; returns what the trust file now says about it."""
    from datetime import datetime, timezone

    real = os.path.realpath(str(path))
    key = _trust_key(real)
    rows = [e for e in trust_entries() if _trust_key(e["path"]) != key]
    removed = len(rows) != len(trust_entries())
    if not remove:
        rows.append({"path": real, "subfolders": bool(subfolders),
                     "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    p = trust_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "trusted": rows}, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return {"path": real, "trusted": not remove, "subfolders": bool(subfolders) and not remove,
            "removed": removed if remove else None, "trust_file": str(p)}


def _merge(cfg: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v


def _without_protected(repo_cfg: dict) -> dict:
    out = {}
    for k, v in repo_cfg.items():
        if k not in PROTECTED_SETTINGS:
            out[k] = v
            continue
        keys = PROTECTED_SETTINGS[k]
        if keys is not None and isinstance(v, dict):
            rest = {kk: vv for kk, vv in v.items() if kk not in keys}
            if rest:
                out[k] = rest
    return out


def _user_config() -> dict:
    data = _read_json(user_config_path())
    return data if isinstance(data, dict) else {}


def _repo_config(repo: Path) -> dict | None:
    """The repository's own config.json: None when missing or unreadable (the defaults then apply)."""
    p = atlas_dir(repo) / "config.json"
    if not p.exists():
        return None
    data = _read_json(p)
    return data if isinstance(data, dict) else None


def load_config(repo: Path) -> dict:
    """The settings for ``repo``: the defaults, then the user-level config (:func:`user_config_path`), then the
    repository's own ``.verinoda/config.json``. The protected settings (:data:`PROTECTED_SETTINGS`) are taken
    from the repository's file only when the user trusts the repository (:func:`is_trusted`);
    :func:`ignored_repo_settings` names the ones left out."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    _merge(cfg, _user_config())
    repo_cfg = _repo_config(repo)
    if repo_cfg:
        _merge(cfg, repo_cfg if is_trusted(repo) else _without_protected(repo_cfg))
    return cfg


def ignored_repo_settings(repo: Path) -> list[str]:
    """The protected settings (``experiments.process_isolation_allowlist``, ``mcp.profile``, ...) the
    repository's own config sets to something other than what is used, because the repository is not
    trusted. Values equal to the ones in use (``verinoda init`` writes the defaults) are not listed."""
    repo_cfg = _repo_config(repo)
    if not repo_cfg or is_trusted(repo):
        return []
    used = load_config(repo)
    out = []
    for section, keys in PROTECTED_SETTINGS.items():
        v = repo_cfg.get(section)
        if v is None:
            continue
        if not isinstance(v, dict):
            if v != used.get(section):
                out.append(section)
            continue
        for k, val in v.items():
            if (keys is None or k in keys) and val != (used.get(section) or {}).get(k):
                out.append(f"{section}.{k}")
    return out


def ignored_settings_note(repo: Path) -> str | None:
    """One sentence for a result: which settings of the repository's config were ignored and what to do."""
    names = ignored_repo_settings(repo)
    if not names:
        return None
    return (f"{', '.join(names)} in the project's .verinoda/config.json "
            f"{'is' if len(names) == 1 else 'are'} ignored: the project is not trusted (a cloned repository can "
            f"ship that file); the user runs `verinoda trust {Path(repo).resolve()}` if they trust it (their "
            f"decision, never an agent's), or sets {'it' if len(names) == 1 else 'them'} in the user config "
            f"{user_config_path()}")
