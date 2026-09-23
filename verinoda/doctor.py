"""`verinoda doctor`: explain the installation and project state.

Never prints secret values: environment variables are reported as set/unset.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from verinoda import __version__

OPTIONAL = {
    "mcp": "MCP server (`verinoda mcp serve`)",
    "tiktoken": "exact token counts in benchmarks (else chars/4 estimate)",
    "anthropic": "benchmark --llm anthropic",
    "pypdf": "PDF documents in research",
    "watchdog": "upstream `verinoda index watch`",
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

        raw = distribution("verinoda").read_text("direct_url.json") or "{}"
        editable = bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:
        pass
    return {"path": str(here.parent), "hardlinked": hardlinked, "editable": editable}


def _check(name: str, ok: bool, detail: str, level: str = "error") -> dict:
    return {"check": name, "ok": ok, "level": "ok" if ok else level, "detail": detail}


def run(repo: Path) -> dict:
    checks: list[dict] = []
    py = sys.version_info
    checks.append(_check("python", py >= (3, 10), f"{sys.version.split()[0]} at {sys.executable}"))
    try:
        from importlib.metadata import distribution

        dist = distribution("verinoda")
        loc = str(dist.locate_file(""))
        checks.append(_check("package", True, f"verinoda {dist.version} installed at {loc}"))
    except Exception:
        checks.append(_check("package", False, "verinoda is importable but not installed as a distribution "
                                               "(running from a source tree?)", "warn"))
    layout = install_layout()
    if layout["editable"] or layout["hardlinked"]:
        why = "editable (dev) install" if layout["editable"] else "package files are hardlinks (uv's default link mode)"
        checks.append(_check("sandbox_readable", False,
                             f"{why}: sandboxed agents (observed with Codex on Windows) may fail to import "
                             "verinoda and must fall back to MCP; reinstall with "
                             "`uv tool install --link-mode copy <wheel-or-path>` for a sandbox-readable copy",
                             "warn"))
    exe = shutil.which("verinoda")
    checks.append(_check("cli_on_path", exe is not None, exe or "`verinoda` not on PATH (use `python -m verinoda`)",
                         "warn"))
    up = Path(__file__).parent / "project_index" / "UPSTREAM_COMMIT"
    base = up.read_text().strip()[:12] if up.exists() else "?"
    graphify = "yes" if importlib.util.find_spec("graphify") else "no"
    checks.append(_check("upstream_base", up.exists(),
                         f"project_index derived from Graphify {base}; graphifyy installed: {graphify} (not required)",
                         "warn"))
    checks.append(_check("git", shutil.which("git") is not None, shutil.which("git") or "git not found: snapshots "
                         "fall back to file hashes, no history view", "warn"))

    # project state
    from verinoda.paths import atlas_dir, db_path, graph_path

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
        checks.append(_check("graph", False, f"no index at {gp}; run `verinoda scan {repo}`", "warn"))
    if db_path(repo).exists():
        from verinoda.snapshot import current_state
        from verinoda.store import SCHEMA_VERSION, Store

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
                                 + ("" if fresh else " - working tree changed; run `verinoda update`"), "warn"))
        st.close()
    else:
        checks.append(_check("database", False, f"no {atlas_dir(repo)}/atlas.db; run `verinoda init`", "warn"))

    # derived indexes, optional resolvers, reference network (round 3)
    if gp.exists():
        proj["search_index"] = _search_index(repo, gp, checks)
        proj["receiver_calls"] = _receiver_sidecar(repo, gp, checks)
    proj["lexicon"] = _lexicon(repo, checks)
    proj["precise"] = _precise(checks)
    proj["tracer"] = _tracer(repo, checks)
    proj["scip"] = _scip(repo, checks)
    proj["references"] = _references(repo, checks)

    # agents
    agents_state: dict = {}
    try:
        from verinoda import agents

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


# -- round-3 state: every probe is read-only apart from what its module records on first use,
#    never raises, and reports sizes, versions and host names - never secret values ----------------

def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"  # pragma: no cover


def _stale_indexed_files(repo: Path, rows: list[tuple]) -> list[str]:
    """Indexed files whose content differs from what the index saw (stat first, sha256 on a stat change)."""
    import hashlib

    out = []
    for f, sha, size, mtime_ns in rows:
        p = repo / f
        try:
            st = p.stat()
        except OSError:
            out.append(f)
            continue
        if size == st.st_size and mtime_ns == st.st_mtime_ns:
            continue
        try:
            if hashlib.sha256(p.read_bytes()).hexdigest() != sha:
                out.append(f)
        except OSError:
            out.append(f)
    return sorted(out)


def _search_index(repo: Path, gp: Path, checks: list[dict]) -> dict:
    """search.db: generation, units, schema/tokenizer versions, graph match, stale files (read only)."""
    import sqlite3

    from verinoda import search_index
    from verinoda.index import same_graph
    from verinoda.paths import search_db_path

    db = search_db_path(repo)
    info: dict = {"path": str(db), "exists": db.exists()}
    if not db.exists():
        checks.append(_check("search_index", False, "no search.db yet: the first query or `verinoda update` "
                             "builds it", "warn"))
        return info
    try:
        conn = sqlite3.connect(str(db), timeout=5)
        try:
            meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
            rows = conn.execute("SELECT file, sha256, size, mtime_ns FROM files").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, ValueError) as exc:
        checks.append(_check("search_index", False, f"unreadable search.db ({type(exc).__name__}); it is "
                             "disposable: the next query rebuilds it", "warn"))
        info["error"] = type(exc).__name__
        return info
    versions_ok = (meta.get("schema_version") == search_index.SCHEMA_VERSION
                   and meta.get("tokenizer_version") == search_index.TOKENIZER_VERSION)
    graph_ok = same_graph(gp, meta.get("graph"))
    stale = _stale_indexed_files(repo, rows)
    info.update(generation=meta.get("generation"), units=meta.get("n_units"), passages=meta.get("n_passages"),
                files=len(rows), size_bytes=db.stat().st_size,
                schema_version=meta.get("schema_version"), tokenizer_version=meta.get("tokenizer_version"),
                supported={"schema_version": search_index.SCHEMA_VERSION,
                           "tokenizer_version": search_index.TOKENIZER_VERSION},
                versions_match=versions_ok, matches_graph=graph_ok, stale_files=len(stale),
                stale_sample=stale[:5])
    detail = (f"generation {meta.get('generation')}, {meta.get('n_units')} units in {len(rows)} files, "
              f"{_fmt_bytes(info['size_bytes'])}, schema v{meta.get('schema_version')} / tokenizer "
              f"v{meta.get('tokenizer_version')}")
    problems = []
    if not versions_ok:
        problems.append(f"built by another version (supported: schema v{search_index.SCHEMA_VERSION}, "
                        f"tokenizer v{search_index.TOKENIZER_VERSION}); the next query rebuilds it")
    if not graph_ok:
        problems.append("describes another graph.json; the next query updates it")
    if stale:
        problems.append(f"{len(stale)} file(s) changed since indexing ({', '.join(stale[:3])}"
                        f"{', ...' if len(stale) > 3 else ''}); run `verinoda update`")
    checks.append(_check("search_index", not problems, detail + ("; " + "; ".join(problems) if problems else ""),
                         "warn"))
    return info


def _receiver_sidecar(repo: Path, gp: Path, checks: list[dict]) -> dict:
    from verinoda.index import RECEIVER_SIDECAR_VERSION, same_graph
    from verinoda.paths import receiver_calls_path

    p = receiver_calls_path(repo)
    info: dict = {"path": str(p), "exists": p.exists()}
    if not p.exists():
        checks.append(_check("receiver_calls", False, "no receiver_calls.json: receiver-call edges are "
                             "computed on every graph load until `verinoda update`/`scan` stores them", "warn"))
        return info
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        checks.append(_check("receiver_calls", False, f"unreadable receiver_calls.json ({type(exc).__name__}); "
                             "recomputed on the next load", "warn"))
        return info
    ok_version = isinstance(data, dict) and data.get("version") == RECEIVER_SIDECAR_VERSION
    match = ok_version and same_graph(gp, data.get("graph"))
    info.update(version=data.get("version") if isinstance(data, dict) else None, matches_graph=match,
                edges=len(data.get("edges") or []) if isinstance(data, dict) else None)
    checks.append(_check("receiver_calls", match,
                         f"{info['edges']} receiver-call edge(s), matches graph.json" if match else
                         "receiver_calls.json does not match graph.json (older version or graph rewritten); "
                         "recomputed on the next load, stored by `verinoda update`", "warn"))
    return info


def _lexicon(repo: Path, checks: list[dict]) -> dict:
    from verinoda import lexicon
    from verinoda.paths import lexicon_path

    try:
        seeds = len(lexicon.seed_entries())
    except Exception as exc:  # noqa: BLE001 - packaging problem: report it, do not fail doctor
        seeds = None
        checks.append(_check("seed_lexicon", False, f"packaged TR->EN seed dictionary unreadable: {exc}"))
    p = lexicon_path(repo)
    info: dict = {"path": str(p), "exists": p.is_file(), "seed_entries": seeds}
    if not p.is_file():
        checks.append(_check("lexicon", False, f"no lexicon.json yet (`verinoda scan` builds it); question "
                             f"words are linked through the seed dictionary only ({seeds} entries)", "info"))
        return info
    info["size_bytes"] = p.stat().st_size
    lex = lexicon.load(repo)
    if lex is None:
        checks.append(_check("lexicon", False, "lexicon.json is from another version or unreadable; "
                             "`verinoda scan` rebuilds it", "warn"))
        return info
    info.update(built_at=lex.built_at, tree_hash=lex.tree_hash, words=len(lex.pairs),
                pairs=sum(len(v) for v in lex.pairs.values()), vocab=len(lex.vocab))
    checks.append(_check("lexicon", True, f"{_fmt_bytes(info['size_bytes'])}, built {lex.built_at}, "
                         f"{info['pairs']} learned pair(s), {info['vocab']} vocabulary words; seed dictionary "
                         f"{seeds} entries", "info"))
    return info


def _precise(checks: list[dict]) -> dict:
    try:
        from verinoda import precise

        ok, why = precise.available()
    except Exception as exc:  # noqa: BLE001
        ok, why = False, f"precise module unavailable: {type(exc).__name__}"
    checks.append(_check("precise", ok, why if ok else f"{why}; call sites stay strong_inference without it",
                         "info"))
    return {"available": ok, "detail": why}


def _tracer(repo: Path, checks: list[dict]) -> dict:
    """Does the interpreter that runs the project's tests have ``sys.monitoring`` (Python 3.12+)?"""
    import subprocess

    from verinoda import experiments

    py = experiments.python_for(repo)
    if os.path.normcase(os.path.abspath(py)) == os.path.normcase(os.path.abspath(sys.executable)):
        has, version = hasattr(sys, "monitoring"), sys.version.split()[0]
    else:
        try:
            r = subprocess.run([py, "-c", "import sys; print(hasattr(sys, 'monitoring'), sys.version.split()[0])"],
                               capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL, check=False)
            parts = r.stdout.split()
            has, version = (parts[0] == "True", parts[1]) if r.returncode == 0 and len(parts) == 2 else (None, None)
        except (OSError, subprocess.SubprocessError):
            has, version = None, None
    if has is None:
        detail = f"could not run the project's Python ({py}); `verinoda observe` will report the failure"
    elif has:
        detail = f"sys.monitoring available in the project's Python {version} ({py})"
    else:
        detail = (f"the project's Python {version} ({py}) has no sys.monitoring: `verinoda observe` falls back "
                  "to sys.setprofile (about 4.5x CPU instead of 1.4x)")
    checks.append(_check("tracer", bool(has), detail, "info"))
    return {"python": py, "version": version, "sys_monitoring": has}


def _scip(repo: Path, checks: list[dict]) -> dict:
    from verinoda import scip_reader
    from verinoda.paths import atlas_dir

    found = scip_reader.find_index(repo)
    if found is None:
        checks.append(_check("scip", False, "no index.scip (optional; cross-language references: "
                             "`verinoda scan <path> --scip FILE`)", "info"))
        return {"index": None}
    info: dict = {"index": str(found), "size_bytes": found.stat().st_size}
    try:
        idx = scip_reader.load(found)
        if atlas_dir(repo).is_dir():
            fresh = scip_reader.document_freshness(repo, found, idx)
        else:  # never create .verinoda/ from doctor: keep the first-read record in a temporary directory
            import tempfile

            with tempfile.TemporaryDirectory(prefix="verinoda-doctor-") as tmp:
                fresh = scip_reader.document_freshness(repo, found, idx, state_dir=Path(tmp))
    except (OSError, ValueError) as exc:
        checks.append(_check("scip", False, f"{found} is unreadable ({type(exc).__name__}: {exc}); precise "
                             "resolution ignores it", "warn"))
        info["error"] = type(exc).__name__
        return info
    n, k = len(fresh), sum(1 for v in fresh.values() if v)
    info.update(tool=f"{idx.tool_name or 'unknown'} {idx.tool_version or ''}".strip(), documents=n, fresh=k,
                fresh_share=round(k / n, 3) if n else None)
    share = f"{k}/{n} documents fresh" + (f" ({100 * k / n:.0f}%)" if n else "")
    checks.append(_check("scip", n > 0 and k == n, f"{found.name} by {info['tool']}: {share}"
                         + ("" if k == n else "; stale documents are ignored - regenerate the index"), "info"))
    return info


def _references(repo: Path, checks: list[dict]) -> dict:
    import time

    from verinoda.paths import http_cache_dir, network_mode

    mode = network_mode(repo)
    cache = http_cache_dir(repo)
    files = size = 0
    if cache.is_dir():
        for p in cache.glob("*.json"):
            if p.name == "hosts.json":
                continue
            try:
                size += p.stat().st_size
                files += 1
            except OSError:
                pass
    blocked: dict[str, str] = {}
    try:
        raw = json.loads((cache / "hosts.json").read_text(encoding="utf-8"))
        for host, until in (raw.get("blocked_until") or {}).items():
            if float(until) > time.time():
                blocked[str(host)] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(until)))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    packaging_ok = importlib.util.find_spec("packaging") is not None
    checks.append(_check("reference_network", True, f"research.network = {mode}; HTTP cache {files} entr"
                         f"{'y' if files == 1 else 'ies'}, {_fmt_bytes(size)}", "info"))
    if blocked:
        checks.append(_check("reference_hosts", False, "rate-limited, no requests until: "
                             + ", ".join(f"{h} ({t})" for h, t in sorted(blocked.items())), "warn"))
    checks.append(_check("packaging", packaging_ok, "importable: PEP 440 version comparison" if packaging_ok else
                         "`packaging` is not importable: version ranges fall back to a numeric parser", "info"))
    return {"network": mode, "http_cache": {"path": str(cache), "entries": files, "size_bytes": size},
            "blocked_hosts": blocked, "packaging": packaging_ok}


def render(res: dict) -> None:
    print(f"verinoda {res['version']}  ({'OK' if res['ok'] else 'PROBLEMS FOUND'})")
    sym = {"ok": "ok  ", "warn": "warn", "error": "FAIL", "info": "info"}
    for c in res["checks"]:
        print(f"  [{sym[c['level']]}] {c['check']}: {c['detail']}")
    if res["project"].get("claims"):
        print("  claims by status: " + ", ".join(f"{k}={v}" for k, v in sorted(res["project"]["claims"].items())))
    print("  secrets (values never shown): " + ", ".join(f"{k}={v}" for k, v in res["env"].items()))
