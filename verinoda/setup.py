"""`verinoda setup`: make a project ready in one command.

It runs the steps a new user would otherwise type one by one, in a safe order,
and every step is idempotent, so running it again is how you refresh a project:

1. ``init`` - create ``.verinoda/`` (database, config); never in the home
   directory itself unless ``allow_home``;
2. ``scan`` on a first run, ``update`` afterwards - index the code (AST only,
   no LLM, no network);
3. agent skills (+ MCP) for the agents that are actually installed on this
   machine (``agents="auto"``), or the ones named, through the same installer
   as ``verinoda install`` (it never overwrites files it does not own);
4. a short checklist of what to do next, including install-layout warnings
   that ``doctor`` would report (for example a hardlinked install that a
   sandboxed agent cannot import).
"""

from __future__ import annotations

from pathlib import Path

AGENTS = ("claude", "codex")
USAGE = {
    "claude": "Claude Code: open this folder, approve the `verinoda` MCP server once (/mcp), then ask "
              "`/verinoda <question>`",
    "codex": "Codex: trust this project so it reads .codex/config.toml, then mention `$verinoda <question>` "
             "(Codex has no /verinoda command)",
}


class SetupRefused(RuntimeError):
    pass


def detect_agents() -> list[str]:
    """Agents whose CLI is on PATH (the same lookup the installer uses)."""
    from verinoda.agents import installer

    return [a for a in AGENTS if installer._which(a)]


def _choose(agents: str | list[str]) -> list[str]:
    if isinstance(agents, (list, tuple)):
        chosen = list(agents)
    elif agents == "auto":
        chosen = detect_agents()
    elif agents == "all":
        chosen = list(AGENTS)
    elif agents == "none":
        chosen = []
    else:
        chosen = [a.strip() for a in agents.split(",") if a.strip()]
    bad = [a for a in chosen if a not in AGENTS]
    if bad:
        raise SetupRefused(f"unknown agent(s): {', '.join(bad)}; choose from {', '.join(AGENTS)}")
    return chosen


def setup_project(path: Path | str = ".", *, agents: str | list[str] = "auto", scope: str = "project",
                  with_mcp: bool = True, allow_home: bool = False, home: Path | None = None) -> dict:
    """Initialise, index and connect agents for one project. Returns a structured report."""
    from verinoda import workflow
    from verinoda.agents import installer
    from verinoda.doctor import install_layout
    from verinoda.paths import _is_home_or_above, graph_path
    from verinoda.store import open_store

    repo = Path(path).resolve()
    if not repo.is_dir():
        raise SetupRefused(f"{repo} is not a directory")
    if _is_home_or_above(repo) and not allow_home:
        raise SetupRefused(f"{repo} is your home directory (or above it); run setup inside a project folder, "
                           "or pass --allow-home if you really mean it")
    if scope not in ("project", "user"):
        raise SetupRefused("scope must be 'project' or 'user'")
    chosen = _choose(agents)

    report: dict = {"repo": str(repo), "steps": [], "agents": [], "warnings": [], "next_steps": []}
    workflow.init(repo)
    report["steps"].append("init")
    st = open_store(repo, create=True)
    try:
        first = not graph_path(repo).exists() or st.latest_snapshot() is None
        res = workflow.scan(st, repo) if first else workflow.update(st, repo)
    finally:
        st.close()
    snap = res.get("snapshot") or {}
    report["index"] = {
        "mode": "scan" if first else res.get("mode", "update"),
        "files": snap.get("file_count"),
        "nodes": (res.get("graph") or {}).get("nodes", snap.get("graph_nodes")),
        "edges": (res.get("graph") or {}).get("edges", snap.get("graph_edges")),
        "stale_claims": len(res.get("stale") or []),
    }
    report["steps"].append(report["index"]["mode"])
    if res.get("error"):
        report["warnings"].append(f"index: {res['error']} (hint: {res.get('hint')})")

    for agent in chosen:
        r = installer.install(agent, scope, project_dir=repo, home=home, with_mcp=with_mcp)
        report["agents"].append({"agent": agent, "scope": scope, "ok": r.get("ok", True),
                                 "result": r.get("result"), "skill": r.get("skill"),
                                 "notes": (r.get("notes") or [])[:3], "manual": (r.get("manual") or [])[:3],
                                 "error": "; ".join(r.get("errors") or []) or None})
        if r.get("ok", True):
            report["next_steps"].append(USAGE[agent])
    if not chosen and agents == "auto":
        missing = [a for a in AGENTS if a not in chosen]
        report["warnings"].append(
            "no coding agent found on PATH (claude, codex); the CLI works on its own. Install an agent later "
            "and re-run `verinoda setup`, or name one: --agents claude")
        report["agents_detected"] = []
        report["agents_missing"] = missing
    lay = install_layout()
    if lay.get("hardlinked") or lay.get("editable"):
        report["warnings"].append(
            "this Verinoda install is hardlinked or editable; a sandboxed agent (Codex on Windows) may not be "
            "able to import it and will fall back to MCP. Reinstall with the install script or "
            "`uv tool install --link-mode copy ...`")
    report["next_steps"].append("Terminal: `verinoda query \"<question>\"` or `verinoda analyze \"<question>\"`; "
                                "after big edits `verinoda update .`; if anything looks wrong `verinoda doctor`")
    report["ok"] = all(a["ok"] for a in report["agents"]) and not res.get("error")
    return report


def render(rep: dict) -> None:
    idx = rep.get("index") or {}
    print(f"Verinoda is set up in {rep['repo']}")
    print(f"  index: {idx.get('mode')} - {idx.get('files')} files, {idx.get('nodes')} nodes, "
          f"{idx.get('edges')} edges" + (f", {idx['stale_claims']} claim(s) marked stale" if idx.get('stale_claims') else ""))
    for a in rep["agents"]:
        state = "ok" if a["ok"] else "FAILED"
        print(f"  {a['agent']} ({a['scope']}): {state} - {a.get('result') or ''}".rstrip(" -"))
        if a.get("error"):
            print(f"    error: {a['error']}")
        for m in a.get("manual") or []:
            print(f"    do by hand: {m}")
    for w in rep["warnings"]:
        print(f"  note: {w}")
    print("Next:")
    for s in rep["next_steps"]:
        print(f"  - {s}")
