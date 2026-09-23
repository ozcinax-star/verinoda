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

``reference=[...]`` also records *reference trees* in the project config
(``index.reference``): code kept for comparison, such as the original
implementation a port is based on, or a vendored copy. Their code ranks lower
unless the question names the tree (see :func:`add_reference`).
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


def add_reference(repo: Path, spec: str) -> dict:
    """Record ``spec`` - ``PATH`` or ``PATH=alias,alias`` - as a reference tree in ``.verinoda/config.json``.

    ``PATH`` is a directory inside the project (relative to it, or absolute). Aliases are
    the words a question uses for the tree ("original", "plugin"); the path's own folder
    names count too. Re-adding a path merges its aliases. Only ``index.reference`` of the
    config file is changed; the ranking reads it at query time, so nothing is re-indexed.
    """
    import json

    from verinoda import textnorm
    from verinoda.paths import atlas_dir

    repo = Path(repo).resolve()
    raw, _, alias_part = spec.rpartition("=") if "=" in spec else (spec, "", "")
    raw = raw.strip().strip('"')
    if not raw:
        raise SetupRefused(f"--reference {spec!r}: no path given (use PATH or PATH=alias,alias)")
    target = Path(raw)
    target = (target if target.is_absolute() else repo / target).resolve()
    try:
        rel = target.relative_to(repo).as_posix()
    except ValueError:
        raise SetupRefused(f"--reference {raw}: not inside the project {repo}") from None
    if not target.is_dir() or rel in ("", "."):
        raise SetupRefused(f"--reference {raw}: not a sub-directory of the project")
    aliases = [textnorm.fold_tr(a.strip()) for a in alias_part.split(",") if a.strip()]
    cfg_p = atlas_dir(repo) / "config.json"
    try:
        cfg = json.loads(cfg_p.read_text(encoding="utf-8")) if cfg_p.exists() else {}
    except (OSError, ValueError) as exc:
        raise SetupRefused(f"{cfg_p} is not readable JSON ({type(exc).__name__}); fix or delete it first") from None
    index_cfg = cfg.setdefault("index", {})
    entries = index_cfg.setdefault("reference", [])
    path = rel.rstrip("/") + "/"
    entry = next((e for e in entries
                  if (e.get("path") if isinstance(e, dict) else e).strip("/") == path.strip("/")), None)
    added = entry is None
    if added:
        entries.append({"path": path, "aliases": sorted(set(aliases))})
    else:
        if not isinstance(entry, dict):  # a plain string entry: keep its position, add aliases
            k = entries.index(entry)
            entries[k] = entry = {"path": path, "aliases": []}
        entry["aliases"] = sorted(set(entry.get("aliases") or []) | set(aliases))
    cfg_p.parent.mkdir(parents=True, exist_ok=True)
    cfg_p.write_bytes((json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return {"path": path, "aliases": (entry or entries[-1]).get("aliases", aliases), "added": added}


CODE_SUFFIXES = (".py", ".java", ".kt", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".cs", ".rb", ".php", ".scala",
                 ".swift", ".c", ".cpp", ".h", ".hpp", ".lua", ".groovy")
MIN_SHARED = 5          # code files a folder must share by name with another folder to look like a copy
MIN_SHARE = 0.5         # ... and that share of its own code files


def reference_suggestions(repo: Path, files: list[str], configured: list[str] = ()) -> list[dict]:
    """Folders that look like a copy of other code in the repository (an original being ported,
    a frozen snapshot): most of their code files share a name with files of one other folder,
    which is larger. Folders already configured as reference trees are left out.

    Returns ``[{"path", "shared", "files", "copy_of"}]``; only a suggestion, never applied.
    """
    from collections import Counter, defaultdict

    from verinoda.architecture_map import is_test_file

    code = [f for f in files if f.endswith(CODE_SUFFIXES) and not is_test_file(f)]
    groups: dict[str, list[str]] = defaultdict(list)
    for f in code:
        parts = f.split("/")
        for depth in (1, 2, 3):
            if len(parts) > depth:
                groups["/".join(parts[:depth]) + "/"].append(f)
    by_name: dict[str, set[str]] = defaultdict(set)
    for f in code:
        by_name[f.rsplit("/", 1)[-1]].add(f)
    done = tuple(c.strip("/") + "/" for c in configured)
    out: list[dict] = []
    for g, members in sorted(groups.items(), key=lambda kv: (kv[0].count("/"), kv[0])):
        seen = done + tuple(o["path"] for o in out)  # configured trees and copies already found
        if len(members) < MIN_SHARED or g.startswith(seen) or any(s.startswith(g) for s in seen):
            continue

        def elsewhere(f: str) -> list[str]:
            return [o for o in by_name[f.rsplit("/", 1)[-1]] if not o.startswith(g) and not o.startswith(seen)]

        shared_files = [f for f in members if elsewhere(f)]
        if len(shared_files) < MIN_SHARED or len(shared_files) < MIN_SHARE * len(members):
            continue
        partners: Counter = Counter(o.split("/")[0] + "/" for f in shared_files for o in elsewhere(f))
        other, _n = partners.most_common(1)[0]
        if len(groups.get(other, [])) <= len(members):
            continue  # the partner is the smaller tree: it would be the copy
        # the deepest sub-folder that still holds nearly all of the shared files
        path = g
        for d in sorted((k for k in groups if k.startswith(g) and k != g), key=lambda k: -k.count("/")):
            if sum(1 for f in shared_files if f.startswith(d)) >= 0.9 * len(shared_files):
                path = d
                break
        out.append({"path": path, "shared": len(shared_files), "files": len(members), "copy_of": other})
    return out


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
                  with_mcp: bool = True, allow_home: bool = False, home: Path | None = None,
                  reference: list[str] | None = None) -> dict:
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
    if reference:
        report["reference"] = [add_reference(repo, spec) for spec in reference]
        report["steps"].append("reference")
    st = open_store(repo, create=True)
    try:
        first = not graph_path(repo).exists() or st.latest_snapshot() is None
        res = workflow.scan(st, repo) if first else workflow.update(st, repo)
    finally:
        st.close()
    snap = res.get("snapshot") or {}
    try:
        from verinoda.paths import load_config
        from verinoda.snapshot import list_files

        configured = [e.get("path") if isinstance(e, dict) else e
                      for e in (load_config(repo).get("index") or {}).get("reference") or []]
        sugg = reference_suggestions(repo, list_files(repo), [c for c in configured if isinstance(c, str)])
    except Exception:  # noqa: BLE001 - a suggestion never fails setup
        sugg = []
    if sugg:
        report["reference_suggestions"] = sugg
        for s in sugg[:3]:
            report["warnings"].append(
                f"{s['path']} looks like a copy of code in {s['copy_of']} ({s['shared']} of its {s['files']} code "
                f"files share a name there); if it is reference code (an original being ported, a snapshot), "
                f"run `verinoda setup --reference {s['path'].rstrip('/')}` so it ranks below your own code")
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
            "able to import it and will fall back to MCP. Reinstall with the command in the README "
            "(`uv tool install --force --reinstall-package verinoda --link-mode copy ...`)")
    report["next_steps"].append("Terminal: `verinoda query \"<question>\"` or `verinoda analyze \"<question>\"`; "
                                "after big edits `verinoda update .`; if anything looks wrong `verinoda doctor`")
    report["ok"] = all(a["ok"] for a in report["agents"]) and not res.get("error")
    return report


def render(rep: dict) -> None:
    idx = rep.get("index") or {}
    print(f"Verinoda is set up in {rep['repo']}")
    print(f"  index: {idx.get('mode')} - {idx.get('files')} files, {idx.get('nodes')} nodes, "
          f"{idx.get('edges')} edges" + (f", {idx['stale_claims']} claim(s) marked stale" if idx.get('stale_claims') else ""))
    for r in rep.get("reference") or []:
        aka = f" (also called: {', '.join(r['aliases'])})" if r.get("aliases") else ""
        print(f"  reference tree: {r['path']}{aka} - ranks lower unless a question names it")
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
