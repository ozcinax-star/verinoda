"""`repoatlas doctor`: explain the installation and project state.

Never prints secret values: environment variables are reported as set/unset.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

from repoatlas import __version__

OPTIONAL = {
    "mcp": "MCP server (`repoatlas mcp serve`)",
    "tiktoken": "exact token counts in benchmarks (else chars/4 estimate)",
    "anthropic": "benchmark --llm anthropic",
    "pypdf": "PDF documents in research",
    "watchdog": "upstream `repoatlas index watch`",
}
SECRET_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN")


def install_layout() -> dict:
    """How the running package is installed.

    ``hardlinked``: the package's own files have more than one link. uv links
    from its cache by default; on Windows the Codex sandbox user was observed
    to get PermissionError reading such files (docs/AGENT-VERIFICATION.md).
    ``editable``: a dev install whose code lives in a source checkout.
    """
    import os

    here = Path(__file__).resolve()
    try:
        hardlinked = os.stat(here).st_nlink > 1
    except OSError:
        hardlinked = False
    editable = False
    try:
        from importlib.metadata import distribution

        raw = distribution("repoatlas").read_text("direct_url.json") or "{}"
        editable = bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:
        pass
    return {"path": str(here.parent), "hardlinked": hardlinked, "editable": editable}


def _check(name: str, ok: bool, detail: str, level: str = "error") -> dict:
    return {"check": name, "ok": ok, "level": "ok" if ok else level, "detail": detail}


def run(repo: Path) -> dict:
    import os

    checks: list[dict] = []
    py = sys.version_info
    checks.append(_check("python", py >= (3, 10), f"{sys.version.split()[0]} at {sys.executable}"))
    try:
        from importlib.metadata import distribution

        dist = distribution("repoatlas")
        loc = str(dist.locate_file(""))
        checks.append(_check("package", True, f"repoatlas {dist.version} installed at {loc}"))
    except Exception:
        checks.append(_check("package", False, "repoatlas is importable but not installed as a distribution "
                                               "(running from a source tree?)", "warn"))
    layout = install_layout()
    if layout["editable"] or layout["hardlinked"]:
        why = "editable (dev) install" if layout["editable"] else "package files are hardlinks (uv's default link mode)"
        checks.append(_check("sandbox_readable", False,
                             f"{why}: sandboxed agents (observed with Codex on Windows) may fail to import "
                             "repoatlas and must fall back to MCP; reinstall with "
                             "`uv tool install --link-mode copy <wheel-or-path>` for a sandbox-readable copy",
                             "warn"))
    exe = shutil.which("repoatlas")
    checks.append(_check("cli_on_path", exe is not None, exe or "`repoatlas` not on PATH (use `python -m repoatlas`)",
                         "warn"))
    up = Path(__file__).parent / "project_index" / "UPSTREAM_COMMIT"
    checks.append(_check("upstream_base", up.exists(),
                         f"project_index derived from Graphify {up.read_text().strip()[:12] if up.exists() else '?'}; "
                         f"graphifyy installed: {'yes' if importlib.util.find_spec('graphify') else 'no'} (not required)",
                         "warn"))
    checks.append(_check("git", shutil.which("git") is not None, shutil.which("git") or "git not found: snapshots "
                         "fall back to file hashes, no history view", "warn"))

    # project state
    from repoatlas.paths import atlas_dir, db_path, graph_path

    proj: dict = {"repo": str(repo)}
    gp = graph_path(repo)
    if gp.exists():
        try:
            data = json.loads(gp.read_text(encoding="utf-8"))
            proj["graph"] = {"path": str(gp), "nodes": len(data.get("nodes", [])),
                             "edges": len(data.get("links", data.get("edges", [])))}
            checks.append(_check("graph", True, f"{proj['graph']['nodes']} nodes, {proj['graph']['edges']} edges"))
        except (OSError, ValueError) as exc:
            checks.append(_check("graph", False, f"unreadable graph: {exc}"))
    else:
        checks.append(_check("graph", False, f"no index at {gp}; run `repoatlas scan {repo}`", "warn"))
    if db_path(repo).exists():
        from repoatlas.snapshot import current_state
        from repoatlas.store import SCHEMA_VERSION, Store

        st = Store(db_path(repo))
        snap = st.latest_snapshot()
        counts = {r["status"]: r["n"] for r in st.all("SELECT status, COUNT(*) AS n FROM claims GROUP BY status")}
        proj["claims"] = counts
        row = st.one("SELECT value FROM meta WHERE key = 'schema_version'")
        proj["schema_version"] = int(row["value"]) if row else None
        proj["schema_version_supported"] = SCHEMA_VERSION
        if snap:
            fresh = current_state(repo)["tree_hash"] == snap["tree_hash"]
            proj["snapshot"] = {"id": snap["id"], "commit": snap["commit_sha"], "created_at": snap["created_at"],
                                "matches_working_tree": fresh}
            checks.append(_check("snapshot", fresh, f"{snap['id']} ({snap['created_at']})"
                                 + ("" if fresh else " - working tree changed; run `repoatlas update`"), "warn"))
        st.close()
    else:
        checks.append(_check("database", False, f"no {atlas_dir(repo)}/atlas.db; run `repoatlas init`", "warn"))

    # agents
    agents_state: dict = {}
    try:
        from repoatlas import agents

        agents_state = agents.status(project_dir=repo)
        for key, st_ in agents_state.items():
            if isinstance(st_, dict) and "installed" in st_:
                checks.append(_check(f"agent:{key}", True,
                                     ("installed" if st_["installed"] else "not installed")
                                     + (f" ({st_.get('skill')})" if st_.get("skill") else "")
                                     + (f"; mcp: {st_.get('mcp')}" if st_.get("mcp") else ""), "info"))
    except Exception as exc:  # agents module optional at runtime
        checks.append(_check("agents", False, f"could not inspect agent installs: {exc}", "warn"))

    opt = {}
    for mod, why in OPTIONAL.items():
        present = importlib.util.find_spec(mod) is not None
        opt[mod] = present
        if not present:
            checks.append(_check(f"optional:{mod}", False, f"missing - needed for {why}", "info"))
    container = next((rt for rt in ("docker", "podman") if shutil.which(rt)), None)
    checks.append(_check("container_isolation", container is not None,
                         container or "no docker/podman: experiments outside the test-runner allowlist are refused",
                         "info"))
    env = {k: ("set" if os.environ.get(k) else "unset") for k in SECRET_ENV}
    ok = all(c["ok"] or c["level"] != "error" for c in checks)
    return {"ok": ok, "version": __version__, "checks": checks, "project": proj, "agents": agents_state,
            "optional": opt, "env": env}


def render(res: dict) -> None:
    print(f"repoatlas {res['version']}  ({'OK' if res['ok'] else 'PROBLEMS FOUND'})")
    sym = {"ok": "ok  ", "warn": "warn", "error": "FAIL", "info": "info"}
    for c in res["checks"]:
        print(f"  [{sym[c['level']]}] {c['check']}: {c['detail']}")
    if res["project"].get("claims"):
        print("  claims by status: " + ", ".join(f"{k}={v}" for k, v in sorted(res["project"]["claims"].items())))
    print("  secrets (values never shown): " + ", ".join(f"{k}={v}" for k, v in res["env"].items()))
