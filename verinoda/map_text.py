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
    if v.get("history_only_targets"):
        out.append(f"   files without a graph node, read in git history only: "
                   f"{', '.join(v['history_only_targets'][:6])}")
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
    coupled, hc = v.get("history_coupled") or [], v.get("history_coupling") or {}
    if coupled:
        out.append(f"changed together in git, no graph edge ({len(coupled)}"
                   + (", more not shown" if hc.get("truncated") else "") + f"; last {hc.get('commits_read')} commits):")
        n = max(3, cap // 4)
        for c in coupled[:n]:
            out.append(f"   [{c['status']}] {c['file']}: {c['commits']} of {c['target_commits']} commits of "
                       f"{c['coupled_to']}")
        out += _more(n, len(coupled), "coupled files")
    if hc.get("error"):
        out.append(f"changed together in git: not read ({hc['error']})")
    elif hc.get("shallow"):
        out.append(f"changed together in git: a shallow clone, only {hc.get('commits_read')} commits to read")
    return out


def _dead(v: dict, cap: int) -> list[str]:
    s, se = v.get("summary", {}), v.get("searched", {})
    claims = v.get("claims", [])
    total = len(claims) + v.get("claims_not_shown", 0)
    out = [f"{total} dead-code claims ({s.get('strong_inference', 0)} strong_inference, "
           f"{s.get('weak_inference', 0)} weak_inference): {s.get('orphan_files', 0)} files, "
           f"{s.get('dead_symbols', 0)} symbols of {s.get('code_symbols', 0)}",
           f"searched from {se.get('entry_points_total', 0)} entry points, {se.get('entry_modules_total', 0)} entry "
           f"modules and {se.get('test_code_nodes', 0)} test-code nodes"]
    n = max(3, cap - 3)
    for c in claims[:n]:
        what = "file" if c["kind"] == "orphan_file" else c.get("symbol_kind", "symbol")
        why = {"orphan_file": "nothing reaches it", "zero_callers": "no callers"}.get(c["kind"], "callers unreached")
        line = f"   [{c['status']}] {what} {c['subject']} at {c['at']}: {why}"
        if c.get("dynamic_uses"):
            u = c["dynamic_uses"][0]
            line += f"; kept alive? {u['kind']} at {u['at']}"
        out.append(line)
    out += _more(min(n, len(claims)), total, "claims (--json has them with their evidence)")
    return out


def _cycles(v: dict, cap: int) -> list[str]:
    """At most ``cap`` lines: a cycle's cuts take two lines each, and a line is kept for saying what was left out."""
    cycles = v.get("cycles", [])
    total = v.get("cycles_total", len(cycles))
    if not total:
        return [f"no dependency cycles between the {v.get('files_with_dependencies', 0)} files with dependencies"]
    exact = v.get("exact_break_sets", sum(c.get("break_method") == "exact" for c in cycles))
    out = [f"{total} cycles over {v.get('files_in_cycles', 0)} files; cutting {v.get('break_set_size', 0)} "
           f"file dependencies breaks them all (the smallest set for {exact} of {total})"]
    for i, c in enumerate(cycles):
        if len(out) + 2 > cap:   # this cycle's header and the line that says how many more
            break
        files = ", ".join(c["files"][:4]) + (f" ... {c['size'] - 4} more" if c["size"] > 4 else "")
        out.append(f"   cycle {i + 1}: {c['size']} files, {c['dependencies_total']} dependencies ({c['status']}"
                   + (f", {c['in']}" if c.get("in") else "") + f"): {files}")
        breaks = c.get("break_set", [])
        n_cuts = c.get("break_set_total", len(breaks))
        room = cap - len(out) - (1 if i + 1 < total else 0)
        m = min(len(breaks), room // 2)
        if m < n_cuts:
            m = min(m, (room - 1) // 2)
        for b in breaks[:max(m, 0)]:
            rel = ", ".join(b.get("relations", {}))
            at = ", ".join(b.get("at", [])[:2])
            refs = f"{b['references']} ref" + ("s" if b["references"] != 1 else "")
            flag = "" if b.get("extracted", 1) else "  [INFERRED edges only]"
            out.append(f"     cut {b['from']} -> {b['to']}  ({refs}: {rel}; {at}){flag}")
            ring = " -> ".join(b["closes"][:6]) + (" ..." if len(b["closes"]) > 6 else "")
            out.append(f"       closes {ring}" + ("  (weak_inference)" if b.get("closes_status") == "weak_inference"
                                                  else ""))
        if n_cuts > max(m, 0):
            out.append(f"     ... {n_cuts - max(m, 0)} {'more ' if m > 0 else ''}cuts in --json")
    else:
        i = len(cycles)
    listed = i if i < len(cycles) else len(cycles)
    if total > listed:
        out.append(f"   ... {total - listed} more cycles" + (" in --json" if listed < len(cycles) else ""))
    return out


RENDERERS = {"hierarchy": _hierarchy, "dependencies": _dependencies, "dataflow": _dataflow,
             "config": _config, "tests": _tests, "history": _history, "impact": _impact, "cycles": _cycles,
             "dead": _dead}


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
