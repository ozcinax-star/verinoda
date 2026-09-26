"""Plain-text summaries of the architecture views (``verinoda map`` without ``--json``).

The views themselves (:mod:`verinoda.architecture_map`) stay the data; this module only decides what a
person reads first: counts, the largest parts, the heaviest dependencies, and how much was left out.
Every truncated list says how many items it did not show.
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath


def _more(shown: int, total: int, what: str) -> list[str]:
    return [f"   ... {total - shown} more {what}"] if total > shown else []


def _hierarchy(v: dict, cap: int) -> list[str]:
    subs = v.get("subsystems", {})
    rows, packages = [], []
    for name, s in subs.items():
        kinds: Counter = Counter()
        symbols = 0
        for pkg, files in s.get("packages", {}).items():
            pkg_syms = 0
            for info in files.values():
                kinds[info.get("kind", "code")] += 1
                pkg_syms += info.get("symbols", 0)
            symbols += pkg_syms
            packages.append((pkg_syms, len(files), pkg))
        rows.append((symbols, s.get("files", 0), name, kinds))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2]))
    total_files = sum(r[1] for r in rows)
    total_syms = sum(r[0] for r in rows)
    out = [f"{total_files} files, {total_syms} symbols in {len(rows)} top-level folders (sorted by symbols)",
           f"   {'folder':<34} {'files':>6} {'code':>6} {'test':>6} {'doc':>5} {'symbols':>8}"]
    n = max(3, cap // 2)
    for syms, files, name, kinds in rows[:n]:
        out.append(f"   {name[:34]:<34} {files:>6} {kinds['code']:>6} {kinds['test']:>6} {kinds['doc']:>5} {syms:>8}")
    out += _more(n, len(rows), "folders")
    packages.sort(key=lambda p: (-p[0], -p[1], p[2]))
    m = max(3, cap - len(out) - 2)
    out.append("largest packages (directories) by symbols:")
    for syms, files, pkg in packages[:m]:
        out.append(f"   {syms:>6} symbols {files:>5} files  {pkg}")
    out += _more(m, len(packages), "packages")
    return out


def _dependencies(v: dict, cap: int) -> list[str]:
    rows = v.get("internal", [])
    out = [f"heaviest {v.get('level', 'file')} dependencies (E = extracted by the parser, I = inferred):"]
    between = v.get("between_subsystems") or []
    n = max(3, (cap - 6) // 2 if between else cap - 4)
    for r in rows[:n]:
        conf = r.get("confidence") or {}
        tag = " ".join(f"{k[0]}{c}" for k, c in sorted(conf.items()))
        out.append(f"   {r['from']} -> {r['to']}  {r.get('relation')} x{r.get('count')} ({tag})")
    out += _more(n, len(rows), "rows in --json (the view keeps the top 60)")
    if between:
        out.append("between top-level folders:")
        for r in between[: max(3, cap - len(out) - 2)]:
            out.append(f"   {r['from']} -> {r['to']}  x{r['count']}")
    ext = v.get("external_imports") or {}
    if ext:
        top = list(ext.items())[:10]
        out.append("most imported outside the project: " + ", ".join(f"{k} ({c})" for k, c in top)
                   + (f", ... {len(ext) - len(top)} more" if len(ext) > len(top) else ""))
    return out


def _dataflow(v: dict, cap: int) -> list[str]:
    entries, sinks, paths = v.get("entries", []), v.get("sinks", []), v.get("paths", [])
    kinds: Counter = Counter(e.get("kind") for s in sinks for e in s.get("evidence", []))
    out = [f"{len(entries)} entry points (heuristic), {len(sinks)} persistence sinks"
           + (" (" + ", ".join(f"{k} {c}" for k, c in kinds.most_common()) + ")" if kinds else "")
           + f", {len(paths)} entry -> sink paths"]
    n = max(3, cap - 3)
    for p in paths[:n]:
        hops = p.get("hops", [])
        via = " -> ".join(h.get("to") or "?" for h in hops[:-1][:3])
        mid = f" via {via}" if via else ""
        out.append(f"   {p['entry']}{mid} -> {p['sink']}  [{', '.join(p.get('sink_kinds', []))}]")
    out += _more(n, len(paths), "paths")
    if not paths and sinks:
        for s in sinks[: max(3, cap - 3)]:
            out.append(f"   sink {s['symbol']} at {s['at']}")
        out += _more(max(3, cap - 3), len(sinks), "sinks")
    return out


def _config(v: dict, cap: int) -> list[str]:
    env, files = v.get("env_vars", {}), v.get("config_files", [])
    out = [f"{len(env)} environment variables read, {len(files)} config files"]
    n = max(3, (cap - 3) // 2)
    for name, reads in sorted(env.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:n]:
        out.append(f"   {name}: {len(reads)} read(s), first at {reads[0]['at']}")
    out += _more(n, len(env), "variables")
    if files:
        by_dir: Counter = Counter(str(PurePosixPath(f).parent) for f in files)
        m = max(3, cap - len(out) - 1)
        out.append("config files:")
        for f in files[:m]:
            out.append(f"   {f}")
        out += _more(m, len(files), f"config files in {len(by_dir)} folders")
    importers = v.get("config_importers") or {}
    if importers:
        out.append("imported config modules: " + ", ".join(f"{k} ({len(u)})" for k, u in list(importers.items())[:6]))
    return out


def _tests(v: dict, cap: int) -> list[str]:
    covered, missing = v.get("covered", {}), v.get("not_reached_by_tests", [])
    out = [f"{v.get('tests', 0)} test functions; {len(covered)} symbols reached by them (static, depth 3)"]
    if v.get("coverage", {}).get("runtime_coverage_file"):
        out.append(f"   runtime coverage file: {v['coverage']['runtime_coverage_file']}")
    n = max(3, cap - 3)
    out.append(f"not reached by any test (first {min(n, len(missing))} of at least {len(missing)}):")
    for m in missing[:n]:
        out.append(f"   {m}")
    return out


def _history(v: dict, cap: int) -> list[str]:
    if not v.get("is_git"):
        return ["no git history"]
    commits, churn = v.get("recent_commits", []), v.get("churn", {})
    out = [f"last {len(commits)} commits:"]
    n = max(3, (cap - 4) // 2)
    for c in commits[:n]:
        out.append(f"   {c['sha'][:10]} {c['date'][:10]} {c['subject'][:90]}")
    out += _more(n, len(commits), "commits")
    if churn:
        out.append("most changed files: " + ", ".join(f"{f} ({c})" for f, c in list(churn.items())[:6]))
    decisions = v.get("decisions", [])
    out.append(f"{len(decisions)} decision documents" + (": " + ", ".join(d["doc"] for d in decisions[:5])
                                                         if decisions else ""))
    return out


def _impact(v: dict, cap: int) -> list[str]:
    syms, files = v.get("affected_symbols", []), v.get("affected_files", {})
    out = [f"targets: {', '.join(v.get('targets', [])[:6]) or '(none)'}"]
    if v.get("unresolved"):
        out.append(f"   not in the graph: {', '.join(v['unresolved'][:6])}")
    out.append(f"{len(syms)} possibly affected symbols in {len(files)} files")
    n = max(3, cap - 5)
    for s in syms[:n]:
        out.append(f"   d{s['distance']} {s['symbol']} at {s['at']}")
    out += _more(n, len(syms), "symbols")
    tests = v.get("tests_to_run", [])
    if tests:
        out.append(f"tests to run ({len(tests)}): " + ", ".join(tests[:6]) + (" ..." if len(tests) > 6 else ""))
    from verinoda import gametests

    out += gametests.render(v.get("gametests"))
    return out


RENDERERS = {"hierarchy": _hierarchy, "dependencies": _dependencies, "dataflow": _dataflow,
             "config": _config, "tests": _tests, "history": _history, "impact": _impact}


def render(res: dict, cap: int) -> str:
    """One block per view: a header with the method, the limits, then the summary lines (at most ``cap``)."""
    out: list[str] = []
    for name, v in res.items():
        out.append(f"== {name} ==  ({v['coverage']['method']})")
        out += [f"   limit: {lim}" for lim in v["coverage"].get("limits", [])]
        body = RENDERERS[name](v, cap) if name in RENDERERS else [f"   {k}: {val}" for k, val in v.items()
                                                                   if k not in ("view", "coverage")]
        out += body[: cap + 2]
        out.append("")
    return "\n".join(out).rstrip() + "\n"
