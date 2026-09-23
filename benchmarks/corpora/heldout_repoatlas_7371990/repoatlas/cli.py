"""`repoatlas` command line.

Every command is a thin adapter over a core function (workflow, analysis,
critique, research, feedback, agents, mcp). The MCP server calls the same
functions, so CLI and MCP never diverge. ``--json`` prints the structured
result; the default is a compact human rendering of the same data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from repoatlas import __version__
from repoatlas.claims import ORDER as _CLAIM_ORDER
from repoatlas.paths import configure_index_env, find_repo_root

configure_index_env()

# Statuses a user may request with `claim add --status` (still downgraded when the
# evidence does not allow them). `stale` is not requestable: only update/scan set it
# from a snapshot diff.
CLAIM_ADD_STATUSES = (*_CLAIM_ORDER, "contradicted")


# -- output helpers -------------------------------------------------------------

def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _emit(args, obj, render=None) -> None:
    if getattr(args, "json", False) or render is None:
        print(_dump(obj))
    else:
        render(obj)


def _repo(args) -> Path:
    r = getattr(args, "repo", None)
    return Path(r).resolve() if r else find_repo_root()


def _store(repo: Path, *, create: bool = False):
    """Existing project state only; ``init``/``scan``/``update`` pass ``create=True``."""
    from repoatlas.store import open_store

    return open_store(repo, create=create)


def _need_graph(repo: Path) -> None:
    from repoatlas.paths import graph_path

    if not graph_path(repo).exists():
        raise SystemExit(f"error: {repo} has no index yet - run `repoatlas scan {repo}` first")


# -- renderers ------------------------------------------------------------------

def _not_challenged_mark(c: dict) -> str:
    """`[not challenged: <reason>]` for a claim whose critique was skipped.

    The reason ("budget" | "claim_limit" | "disabled") comes from the analysis
    (``not_challenged_reason``); when a result carries only ``challenged: false``
    no reason is guessed.
    """
    reason = c.get("not_challenged_reason")
    if reason:
        return f"  [not challenged: {reason}]"
    return "  [not challenged]" if c.get("challenged", True) is False else ""


def _r_claims(res: dict) -> None:
    print(f"analysis {res['analysis_id']}  snapshot {res['snapshot']['id']} "
          f"commit {(res['snapshot']['commit'] or 'no-git')[:10]}{' (dirty)' if res['snapshot']['dirty'] else ''}")
    print(f"intents: {', '.join(res['intents'])}\n")
    for c in res["claims"]:
        print(f"[{c['status']} {c['confidence']:.2f}] {c['text']}  ({c['id']}){_not_challenged_mark(c)}")
        for e in c["evidence"][:3]:
            print(f"      {e}")
        for u in c["uncertainties"][:2]:
            print(f"      ? {u}")
    if res["unknowns"]:
        print("\nunknown:")
        for u in res["unknowns"]:
            print(f"  - {u['question']}: {u['why']}\n    next: {u['next_step']}")
    if res.get("critique"):
        print("\ncritique:")
        for c in res["critique"]:
            print(f"  {c['claim']}: {c['before']} -> {c['after']}  " + "; ".join((c["fails"] + c["warns"])[:2]))
    u = res["usage"]
    print(f"\nusage: {u['elapsed_s']}s, {u['tool_calls']} tool calls, ~{u['context_tokens_est']} tokens "
          f"({u['token_count_method']}){'; budget: ' + u['exhausted'] if u['exhausted'] else ''}")


def _r_query(res: dict) -> None:
    print(f"terms: {res['terms']}")
    for it in res["items"]:
        ln = f":{it['lines'][0]}-{it['lines'][1]}" if it.get("lines") else ""
        print(f"\n{it['symbol']}  {it['file']}{ln}  score={it['score']}")
        for w in it["why"]:
            print(f"   why: {w}")
        for l in (it["excerpt"] or "").splitlines()[:6]:
            print(f"   | {l}")
    if res["edges"]:
        print("\nrelations:")
        for e in res["edges"]:
            print(f"  {e['from']} -{e['relation']}[{e['confidence']}]-> {e['to']}  @{e['at']}")
    b = res["budget"]
    print(f"\n{b['used_chars']}/{b['max_chars']} chars{' (truncated)' if b['truncated'] else ''}")


def _r_trace(res: dict) -> None:
    print(f"{res['source']} -> {res['target']}  [{res['status']}, mode={res['mode']}]")
    for i, p in enumerate(res["paths"], 1):
        print(f" path {i}:")
        for h in p:
            extra = f" derived_by={h['derived_by']}" if h.get("derived_by") else ""
            print(f"   {h['from']} -{h['relation']}[{h['confidence']}]-> {h['to']}  @{h['at']}{extra}")
    if res.get("undirected_hint"):
        print(" undirected connection only: " + " - ".join(res["undirected_hint"]))


def _r_claim(res: dict) -> None:
    print(f"{res['id']}  [{res['status']} {res['confidence']:.2f}]  kind={res['kind']}")
    print(f"  {res['text']}")
    print(f"  snapshot {res['snapshot_id']} commit {(res['commit_sha'] or '-')[:10]} valid_version {(res['valid_version'] or '-')[:10]}")
    for k in ("supporting", "refuting", "qualifying"):
        for e in res[k]:
            print(f"  {k[:-3] if k.endswith('ing') else k}: {e['type']} {e['at']} {e.get('hash') or ''}")
    for u in res["uncertainties"]:
        print(f"  ? {u}")
    print("  history:")
    for h in res["history"]:
        print(f"    {h['created_at']} {h['from_status']} -> {h['to_status']} ({h['to_confidence']:.2f}) {h['actor']}: {h['reason']}")


def _r_challenge(res: dict) -> None:
    print(f"{res['claim']}: {res['before']['status']} {res['before']['confidence']:.2f} -> "
          f"{res['after']['status']} {res['after']['confidence']:.2f}")
    for f in res["findings"]:
        print(f"  [{f['result']}] {f['check']}: {f['detail']}")
    for a in res["alternatives"]:
        print(f"  alternative: {a}")


# -- commands -------------------------------------------------------------------

def cmd_doctor(args) -> int:
    from repoatlas.doctor import render, run

    res = run(_repo(args))
    _emit(args, res, render)
    return 0 if res["ok"] else 1


def cmd_init(args) -> int:
    from repoatlas import workflow

    res = workflow.init(Path(args.path or ".").resolve())
    _emit(args, res, lambda r: print(f"initialised {r['atlas_dir']}"))
    return 0


def cmd_scan(args) -> int:
    from repoatlas import workflow

    repo = Path(args.path).resolve()
    workflow.init(repo)
    st = _store(repo)
    res = workflow.scan(st, repo, force=args.force)

    def render(r):
        print(f"indexed {repo.name}: {r['graph']['nodes']} nodes, {r['graph']['edges']} edges in "
              f"{r['index_seconds']}s\n"
              f"snapshot {r['snapshot']['id']} commit {(r['snapshot']['commit_sha'] or 'no-git')[:10]} "
              f"files {r['snapshot']['file_count']}; {len(r['stale'])} claim(s) marked stale")
        for w in r.get("warnings") or []:
            print(f"  warning: {w}")
    _emit(args, res, render)
    return 0


def _r_update(r: dict) -> None:
    """Human rendering of ``workflow.update``; tolerant of partial/error results."""
    snap = r.get("snapshot") or {}
    parts = [f"{r.get('mode', 'unknown')}: {r.get('changed_count') or 0} changed file(s)"]
    if r.get("index_mode"):
        parts.append(f"index {r['index_mode']}")
    if snap.get("id"):
        parts.append(f"snapshot {snap['id']}")
    print("; ".join(parts))
    for s in r.get("stale") or []:
        print(f"  stale: {s['id']} {s['text'][:90]}  <- {', '.join(c['file'] for c in s.get('changed') or [])}")
    for w in r.get("warnings") or []:
        print(f"  warning: {w}")
    if r.get("error"):
        print(f"error: {r['error']}", file=sys.stderr)
    if r.get("hint"):
        print(f"  hint: {r['hint']}", file=sys.stderr)


def cmd_update(args) -> int:
    from repoatlas import workflow

    repo = Path(args.path).resolve()
    st = _store(repo, create=True)
    res = workflow.update(st, repo)
    _emit(args, res, _r_update)
    return 1 if res.get("error") else 0


def cmd_map(args) -> int:
    from repoatlas import architecture_map as am
    from repoatlas import index

    repo = Path(args.path).resolve()
    _need_graph(repo)
    g = index.load(repo)
    if args.view == "impact":
        targets = args.target or am.changed_files_from_git(repo, args.base)
        res = {"impact": am.impact(g, targets)}
    elif args.view:
        res = {args.view: am.VIEWS[args.view](g)}
    else:
        res = am.build_map(g)
    if args.json:
        print(_dump(res))
        return 0
    for name, v in res.items():
        print(f"== {name} ==  ({v['coverage']['method']})")
        for lim in v["coverage"].get("limits", []):
            print(f"   limit: {lim}")
        body = {k: val for k, val in v.items() if k not in ("view", "coverage")}
        text = _dump(body)
        lines = text.splitlines()
        print("\n".join(lines[: args.max_lines]))
        if len(lines) > args.max_lines:
            print(f"   ... {len(lines) - args.max_lines} more lines (use --json or --view)")
    return 0


def cmd_query(args) -> int:
    from repoatlas import index, retrieval

    repo = _repo(args)
    _need_graph(repo)
    g = index.load(repo)
    res = retrieval.retrieve(g, args.question, retrieval.Budget(max_items=args.max_items, max_chars=args.max_chars))
    _emit(args, res, _r_query)
    return 0


def cmd_trace(args) -> int:
    from repoatlas import index, retrieval

    repo = _repo(args)
    _need_graph(repo)
    res = retrieval.trace(index.load(repo), args.source, args.target, mode=args.mode)
    _emit(args, res, _r_trace)
    return 0 if res["status"] == "found" else 2


def cmd_analyze(args) -> int:
    from repoatlas import analysis

    repo = _repo(args)
    _need_graph(repo)
    b = analysis.Budget(seconds=args.budget_seconds, tool_calls=args.budget_calls,
                        context_tokens=args.budget_tokens)
    res = analysis.analyze(_store(repo), repo, args.question, budget=b, run_tests=args.run_tests,
                           challenge=not args.no_challenge)
    _emit(args, res, _r_claims)
    return 0


def cmd_claim(args) -> int:
    from repoatlas.claims import Claims

    repo = _repo(args)
    st = _store(repo)
    if args.claim_cmd == "show":
        try:
            res = Claims(st, repo).show(args.id)
        except KeyError as exc:
            raise SystemExit(f"error: {exc}")
        _emit(args, res, _r_claim)
    elif args.claim_cmd == "list":
        rows = st.claims(status=args.status, limit=args.limit)
        res = [{k: r[k] for k in ("id", "status", "confidence", "kind", "text", "updated_at")} for r in rows]
        _emit(args, res, lambda rs: [print(f"{r['id']} [{r['status']} {r['confidence']:.2f}] {r['text'][:100]}") for r in rs])
    elif args.claim_cmd == "add":
        from repoatlas import evidence as evmod
        from repoatlas.snapshot import take_snapshot

        snap = st.latest_snapshot() or take_snapshot(st, repo)
        evs = []
        for spec in args.source or []:
            path, _, rng = spec.rpartition(":")
            a, _, b = rng.partition("-")
            if not path or not a.isdigit() or (b and not b.isdigit()):
                raise SystemExit(f"error: --source {spec!r} must look like path/to/file.py:START[-END]")
            ev = evmod.source_evidence(repo, path, int(a), int(b or a), commit=snap["commit_sha"])
            if ev is None:
                raise SystemExit(f"error: {spec} does not exist")
            evs.append((ev, "refutes" if args.refutes else "supports"))
        c = Claims(st, repo).create(args.text, project=snap["project"], snapshot=snap, status=args.status,
                                    evidence=evs, subjects=[s.rpartition(":")[0] for s in args.source or []],
                                    actor="user")
        res = Claims(st, repo).show(c["id"])
        _emit(args, res, _r_claim)
    return 0


def cmd_verify(args) -> int:
    from repoatlas import workflow

    repo = _repo(args)
    res = workflow.verify(_store(repo), repo, args.id, run=args.run)

    def render(r):
        print(f"{r['claim']}: {r['before']['status']} -> {r['after']['status']} ({r['after']['confidence']:.2f})")
        for c in r["source_checks"]:
            print(f"  {'ok ' if c['ok'] else 'BAD'} {c['at']}: {c['reason']}")
        if r["experiment"]:
            e = r["experiment"]
            print(f"  experiment {e['id']}: {e['outcome']} ({e['isolation']}), logs {e['logs']['stdout']}")
    _emit(args, res, render)
    return 0


def cmd_challenge(args) -> int:
    from repoatlas import critique, index

    from repoatlas.paths import graph_path

    repo = _repo(args)
    g = index.load(repo) if graph_path(repo).exists() else None
    res = critique.challenge(_store(repo), repo, args.id, graph=g)
    _emit(args, res, _r_challenge)
    return 0


def cmd_research(args) -> int:
    from repoatlas import research

    repo = _repo(args)
    res = research.research(_store(repo, create=True), repo, args.reference, ref=args.ref, topic=args.topic,
                            kind=args.kind)
    _emit(args, res, research.render)
    return 0 if res.get("status") == "ok" else 1


def cmd_compare(args) -> int:
    from repoatlas import research

    local = Path(args.local).resolve()
    res = research.compare(_store(local, create=True), local, args.reference, ref=args.ref, topic=args.topic)
    _emit(args, res, research.render_compare)
    return 0 if res.get("status") == "ok" else 1


def cmd_feedback(args) -> int:
    from repoatlas import feedback

    repo = _repo(args)
    st = _store(repo)
    if args.fb_cmd == "add":
        res = feedback.add(st, repo, args.text, claim_id=args.claim, reference=args.reference, ref=args.ref,
                           correction=args.correction, expect_pattern=args.expect_pattern,
                           expect_in=args.expect_in)
        if args.process:
            res = feedback.process(st, repo, res["id"], topic=args.topic)
    elif args.fb_cmd == "process":
        res = feedback.process(st, repo, args.id, topic=args.topic)
    elif args.fb_cmd == "resolve":
        res = feedback.resolve(st, repo, args.id, args.verdict, reason=args.reason,
                               evidence_ids=args.evidence or [], correction=args.correction)
    elif args.fb_cmd == "show":
        res = feedback.show(st, args.id)
    else:
        res = feedback.list_feedback(st)
    _emit(args, res, feedback.render)
    if args.fb_cmd == "resolve" and isinstance(res, dict) and res.get("status") == "rejected":
        return 3  # nothing changed: the verdict did not meet the evidence rules
    return 0


def cmd_experiment(args) -> int:
    from repoatlas import experiments

    repo = _repo(args)
    argv = args.command
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise SystemExit("error: give the command after --")
    try:
        res = experiments.run(_store(repo, create=True), repo, argv, hypothesis=args.hypothesis, expect=args.expect,
                              timeout=args.timeout, claim_id=args.claim, isolation=args.isolation)
    except experiments.ExperimentRefused as exc:
        _emit(args, {"status": "refused", "reason": str(exc)}, lambda r: print(f"refused: {r['reason']}"))
        return 3

    def render(r):
        s = r["summary"]
        print(f"{r['id']}: {r['outcome']} in {r['duration_s']}s  isolation={r['isolation']} "
              f"(matches expectation: {r['matches_expectation']})")
        for l in s["summary_lines"] or s["tail"][-3:]:
            print(f"  {l}")
        for l in s["relevant_lines"][:8]:
            print(f"  ! {l}")
        print(f"  evidence {r['evidence_id']}; full logs: {r['logs']['stdout']}")
        print("  isolation guarantees: " + ", ".join(k for k, v in r["guarantees"].items() if v)
              + "; NOT: " + ", ".join(k for k, v in r["guarantees"].items() if not v))
    _emit(args, res, render)
    return 0


def cmd_memory(args) -> int:
    from repoatlas.memory import Memory

    repo = _repo(args)
    st = _store(repo)
    m = Memory(st)
    if args.mem_cmd == "learn":
        src = st.claim(args.claim) if args.claim else None
        try:
            res = m.learn(args.key, args.value, source_claim_id=args.claim,
                          snapshot_id=(src or {}).get("snapshot_id"))
        except ValueError as exc:
            raise SystemExit(f"error: {exc}")
    elif args.mem_cmd == "history":
        res = m.history(args.key)
    else:
        res = m.recall(args.key)
    print(_dump(res))
    return 0


def cmd_install(args) -> int:
    from repoatlas import agents

    res = agents.install(args.agent, args.scope, project_dir=Path(args.project_dir or ".").resolve(),
                         with_mcp=not args.no_mcp, dry_run=args.dry_run)
    _emit(args, res, agents.render)
    return 0 if res.get("ok", True) else 1


def cmd_uninstall(args) -> int:
    from repoatlas import agents

    res = agents.uninstall(args.agent, args.scope, project_dir=Path(args.project_dir or ".").resolve(),
                           dry_run=args.dry_run)
    _emit(args, res, agents.render)
    return 0 if res.get("ok", True) else 1


def cmd_mcp(args) -> int:
    from repoatlas.mcp.server import serve

    serve(_repo(args))
    return 0


# Upstream subcommands that write Graphify-branded skills, hooks, merge drivers or
# agent configs into the user's home or project (CLAUDE.md, AGENTS.md, .claude/,
# .codex/, git hooks, .gitattributes, ...). Run through `repoatlas index` they would
# overwrite or remove a REAL Graphify install, so they are refused. Taken from the
# upstream dispatch: install.py `_CLI_INSTALL_COMMANDS` (install, uninstall and every
# platform installer incl. the `skills` alias) plus cli.py `hook` and `merge-driver`.
# `_blocked_index_commands()` also unions the live upstream set, so a platform added
# upstream is blocked without editing this list.
UPSTREAM_BLOCKED = frozenset({
    "install", "uninstall", "hook", "merge-driver",
    "agents", "aider", "amp", "antigravity", "claude", "claw", "codebuddy", "codex", "copilot", "cursor",
    "devin", "droid", "gemini", "hermes", "kilo", "kiro", "opencode", "pi", "skills", "trae", "trae-cn",
    "vscode",
})


def _blocked_index_commands() -> frozenset:
    try:
        from repoatlas.project_index.install import _CLI_INSTALL_COMMANDS
    except Exception:  # pragma: no cover - upstream layout changed; the static list still applies
        return UPSTREAM_BLOCKED
    return UPSTREAM_BLOCKED | frozenset(_CLI_INSTALL_COMMANDS)


def cmd_index(args) -> int:
    """Pass-through to the Graphify-derived CLI (advanced/upstream features).

    Installer-type subcommands are refused (exit 2): see ``UPSTREAM_BLOCKED``.
    """
    rest = args.rest[1:] if args.rest and args.rest[0] == "--" else args.rest
    if rest and rest[0] in _blocked_index_commands():
        print(f"error: `repoatlas index {rest[0]}` is blocked: this upstream Graphify command writes "
              "Graphify-branded skills, hooks, merge drivers or agent configs and could overwrite or remove "
              "a real Graphify install.\n"
              "  Use `repoatlas install --agent claude|codex --scope project|user` for the RepoAtlas skill + MCP "
              "(and `repoatlas uninstall` to remove exactly what it added).", file=sys.stderr)
        return 2
    from repoatlas.project_index.__main__ import main as upstream_main

    sys.argv = ["repoatlas index", *rest]
    upstream_main()
    return 0


def cmd_benchmark(args) -> int:
    from repoatlas.benchmark import run_benchmark

    res = run_benchmark(Path(args.repo or ".").resolve(), questions=args.questions, out=args.out,
                        graphify_cmd=args.graphify_cmd, llm=args.llm, repeat=args.repeat)
    from repoatlas.benchmark import render

    _emit(args, res, render)
    return 0


# -- parser ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repoatlas",
        description="Evidence-first codebase analysis (derived from Graphify; not an official Graphify release).",
    )
    p.add_argument("--version", action="version", version=f"repoatlas {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    def add(name, fn, help_, repo=True, js=True):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        if repo:
            sp.add_argument("--repo", help="project root (default: nearest dir with .repoatlas or .git)")
        if js:
            sp.add_argument("--json", action="store_true", help="print structured JSON")
        return sp

    add("doctor", cmd_doctor, "check installation, index, agent skills and MCP configuration")
    sp = add("init", cmd_init, "create .repoatlas/ (database + config) in a project", repo=False)
    sp.add_argument("path", nargs="?", default=".")
    sp = add("scan", cmd_scan, "index a repository (AST, no LLM) and record a snapshot", repo=False)
    sp.add_argument("path")
    sp.add_argument("--force", action="store_true", help="rebuild even if the graph shrinks")
    sp = add("update", cmd_update, "re-index changed files, record a snapshot, mark affected claims stale", repo=False)
    sp.add_argument("path")
    sp = add("map", cmd_map, "top-down architecture views", repo=False)
    sp.add_argument("path")
    sp.add_argument("--view", choices=["hierarchy", "dependencies", "dataflow", "config", "tests", "history", "impact"])
    sp.add_argument("--target", action="append", help="impact view: file or symbol (repeatable); default: git changes")
    sp.add_argument("--base", help="impact view: diff base (default HEAD + untracked)")
    sp.add_argument("--max-lines", type=int, default=60)
    sp = add("query", cmd_query, "bounded, justified retrieval for a question")
    sp.add_argument("question")
    sp.add_argument("--max-items", type=int, default=10)
    sp.add_argument("--max-chars", type=int, default=6000)
    sp = add("trace", cmd_trace, "directed paths between two symbols/files with edge locations")
    sp.add_argument("source")
    sp.add_argument("target")
    sp.add_argument("--mode", choices=["flow", "any"], default="flow")
    sp = add("analyze", cmd_analyze, "answer a question as claims with evidence, critique and unknowns")
    sp.add_argument("question")
    sp.add_argument("--run-tests", action="store_true", help="run the tests that reach the answer (isolated)")
    sp.add_argument("--no-challenge", action="store_true")
    sp.add_argument("--budget-seconds", type=float, default=60)
    sp.add_argument("--budget-calls", type=int, default=40)
    sp.add_argument("--budget-tokens", type=int, default=6000)

    sp = sub.add_parser("claim", help="inspect or add claims")
    csub = sp.add_subparsers(dest="claim_cmd", required=True)
    for name in ("show", "list", "add"):
        c = csub.add_parser(name)
        c.set_defaults(fn=cmd_claim)
        c.add_argument("--repo")
        c.add_argument("--json", action="store_true")
        if name == "show":
            c.add_argument("id")
        elif name == "list":
            c.add_argument("--status")
            c.add_argument("--limit", type=int, default=50)
        else:
            c.add_argument("text")
            c.add_argument("--source", action="append", help="file:start[-end] evidence (repeatable)")
            c.add_argument("--refutes", action="store_true", help="the sources refute rather than support")
            c.add_argument("--status", default="statically_verified", choices=CLAIM_ADD_STATUSES,
                           help="requested status; downgraded if the evidence does not allow it "
                                "(contradicted needs --refutes sources; stale is set only by update/scan)")

    sp = add("verify", cmd_verify, "re-check a claim's evidence against the current tree")
    sp.add_argument("id")
    sp.add_argument("--run", action="store_true", help="also re-run the claim's recorded test experiment")
    sp = add("challenge", cmd_challenge, "adversarial check; lowers confidence when evidence is weak")
    sp.add_argument("id")
    sp = add("research", cmd_research, "inspect a reference repo (pinned commit) or document URL")
    sp.add_argument("reference")
    sp.add_argument("--ref", help="branch/tag/commit to pin (default: the reference's default branch HEAD)")
    sp.add_argument("--topic", help="mechanism to trace in the reference")
    sp.add_argument("--kind", choices=["auto", "official_doc", "standard", "paper", "secondary", "reference_repo"],
                    default="auto")
    sp = add("compare", cmd_compare, "compare a mechanism's assumptions between a local repo and a reference", repo=False)
    sp.add_argument("local")
    sp.add_argument("reference")
    sp.add_argument("--ref")
    sp.add_argument("--topic", required=True)

    sp = sub.add_parser("feedback", help="user critique of earlier claims, processed as hypotheses")
    fsub = sp.add_subparsers(dest="fb_cmd", required=True)
    for name in ("add", "process", "resolve", "show", "list"):
        c = fsub.add_parser(name)
        c.set_defaults(fn=cmd_feedback)
        c.add_argument("--repo")
        c.add_argument("--json", action="store_true")
        if name == "add":
            c.add_argument("--text", required=True)
            c.add_argument("--claim")
            c.add_argument("--reference", help="repo URL/path, document URL")
            c.add_argument("--ref")
            c.add_argument("--correction", help="the corrected statement the user proposes")
            c.add_argument("--expect-pattern", help="regex the user says occurs (checkable proposition)")
            c.add_argument("--expect-in", help="file glob where --expect-pattern should occur")
            c.add_argument("--topic")
            c.add_argument("--process", action="store_true", help="run the verification protocol now")
        elif name == "process":
            c.add_argument("id")
            c.add_argument("--topic")
        elif name == "resolve":
            c.add_argument("id")
            c.add_argument("--verdict", required=True, choices=["confirmed", "qualified", "corrected", "unresolved"])
            c.add_argument("--reason", required=True)
            c.add_argument("--evidence", action="append")
            c.add_argument("--correction")
        elif name == "show":
            c.add_argument("id")

    sp = sub.add_parser("experiment", help="run a targeted experiment with recorded isolation")
    esub = sp.add_subparsers(dest="exp_cmd", required=True)
    c = esub.add_parser("run")
    c.set_defaults(fn=cmd_experiment)
    c.add_argument("--repo")
    c.add_argument("--json", action="store_true")
    c.add_argument("--hypothesis", required=True)
    c.add_argument("--expect", choices=["pass", "fail"], default="pass")
    c.add_argument("--timeout", type=float)
    c.add_argument("--claim")
    c.add_argument("--isolation", choices=["auto", "process", "container"], default="auto")
    c.add_argument("command", nargs=argparse.REMAINDER)

    sp = sub.add_parser("memory", help="versioned learnings tied to claims")
    msub = sp.add_subparsers(dest="mem_cmd", required=True)
    for name in ("list", "learn", "history"):
        c = msub.add_parser(name)
        c.set_defaults(fn=cmd_memory)
        c.add_argument("--repo")
        if name == "learn":
            c.add_argument("key")
            c.add_argument("value")
            c.add_argument("--claim")
        elif name == "history":
            c.add_argument("key")
        else:
            c.add_argument("key", nargs="?")

    for name, fn, help_ in (("install", cmd_install, "install the RepoAtlas skill (+MCP) for an agent"),
                            ("uninstall", cmd_uninstall, "remove only what `install` added")):
        sp = add(name, fn, help_, repo=False)
        sp.add_argument("--agent", required=True, choices=["claude", "codex"])
        sp.add_argument("--scope", required=True, choices=["project", "user"])
        sp.add_argument("--project-dir")
        sp.add_argument("--dry-run", action="store_true")
        if name == "install":
            sp.add_argument("--no-mcp", action="store_true", help="skill only, no MCP registration")

    sp = sub.add_parser("mcp", help="MCP server for coding agents")
    msub = sp.add_subparsers(dest="mcp_cmd", required=True)
    c = msub.add_parser("serve", help="serve over stdio")
    c.set_defaults(fn=cmd_mcp)
    c.add_argument("--repo")

    sp = sub.add_parser("index", help="pass-through to the Graphify-derived indexer CLI (advanced; "
                                       "installer/hook commands are blocked - use `repoatlas install`)")
    sp.set_defaults(fn=cmd_index)
    sp.add_argument("rest", nargs=argparse.REMAINDER)

    sp = sub.add_parser("benchmark", help="reproducible comparison: raw search vs Graphify baseline vs RepoAtlas")
    bsub = sp.add_subparsers(dest="bench_cmd", required=True)
    c = bsub.add_parser("run")
    c.set_defaults(fn=cmd_benchmark)
    c.add_argument("--repo")
    c.add_argument("--json", action="store_true")
    c.add_argument("--questions", help="questions JSON (default: built-in set for the repo)")
    c.add_argument("--out", help="write results JSON here")
    c.add_argument("--graphify-cmd", help="external `graphify` executable for a true upstream baseline")
    c.add_argument("--llm", choices=["none", "anthropic"], default="none",
                   help="also have a model answer from each context (needs ANTHROPIC_API_KEY)")
    c.add_argument("--repeat", type=int, default=2, help="runs per question (first = cold, rest = warm)")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    try:
        return int(args.fn(args) or 0)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, re.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
