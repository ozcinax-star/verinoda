"""`verinoda` command line.

Every command is a thin adapter over a core function (workflow, analysis,
critique, research, feedback, references, question_plan, precise, runtime,
agents, mcp). The MCP server calls the same functions, so CLI and MCP never
diverge. ``--json`` prints the structured result; the default is a compact
human rendering of the same data (``query`` prints the plain-text context the
model reads, docs/DESIGN.md D20).

Exit codes: 0 done; 1 error; 2 usage error, invalid plan, unresolved trace
endpoint or blocked upstream command; 3 "needs more": a plan that needs
clarification, a partial reference resolution, a refused experiment, an
incomplete observation, no precise answer.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from verinoda import __version__
from verinoda.claims import ORDER as _CLAIM_ORDER
from verinoda.paths import configure_index_env, find_repo_root

configure_index_env()

# Statuses a user may request with `claim add --status` (still downgraded when the
# evidence does not allow them). `stale` is not requestable: only update/scan set it
# from a snapshot diff.
CLAIM_ADD_STATUSES = (*_CLAIM_ORDER, "contradicted")
# Claim kinds a user may give to `claim add`: each has a mechanical grader in
# verinoda.entail (`--symbol` feeds its spec), so the claim is graded by the kind's
# predicate instead of by term coverage.
CLAIM_ADD_KINDS = ("general", "location", "relation", "config")
# `verinoda analyze` / `plan check` status -> exit code.
PLAN_EXIT = {"ready": 0, "answered": 0, "invalid": 2, "invalid_plan": 2, "needs_clarification": 3, "no_index": 1}
TRACE_MODES = ("auto", "monitoring", "setprofile", "off")   # verinoda.runtime.trace.MODES
NETWORK_CHOICES = ("off", "cache", "on")                      # verinoda.paths.NETWORK_MODES
OBSERVE_LIST_CAP = 10


# -- output helpers -------------------------------------------------------------

def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _write(text: str) -> None:
    """Print ``text`` whatever the console's code page (rendered source lines may hold any character).

    ``main`` reconfigures stdout to UTF-8 with ``errors='replace'``; when a
    stream refuses that (or is replaced), the text goes to its binary buffer as
    UTF-8 instead of raising ``UnicodeEncodeError`` half-way through.
    """
    if not text.endswith("\n"):
        text += "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        buf = getattr(sys.stdout, "buffer", None)
        if buf is None:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            sys.stdout.write(text.encode(enc, "replace").decode(enc, "replace"))
        else:
            sys.stdout.flush()
            buf.write(text.encode("utf-8", "replace"))
            buf.flush()


def _emit(args, obj, render=None) -> None:
    if getattr(args, "json", False) or render is None:
        _write(_dump(obj))
    else:
        render(obj)


def _repo(args) -> Path:
    r = getattr(args, "repo", None)
    return Path(r).resolve() if r else find_repo_root()


def _store(repo: Path, *, create: bool = False):
    """Existing project state only; ``init``/``scan``/``update`` pass ``create=True``."""
    from verinoda.store import open_store

    return open_store(repo, create=create)


def _need_graph(repo: Path) -> None:
    from verinoda.paths import graph_path

    if not graph_path(repo).exists():
        raise SystemExit(f"error: {repo} has no index yet - run `verinoda scan {repo}` first")


def _rel_in_repo(repo: Path, path: str, what: str) -> str:
    """A repository-relative posix path for ``path`` (repo-relative, cwd-relative or absolute)."""
    p = Path(path)
    if not p.is_absolute() and not (repo / p).exists() and (Path.cwd() / p).exists():
        p = Path.cwd() / p
    if p.is_absolute():
        try:
            return p.resolve().relative_to(repo).as_posix()
        except ValueError:
            raise SystemExit(f"error: {what} {path} is outside the repository {repo}")
    return path.replace("\\", "/")


def _path_line(repo: Path, spec: str, what: str) -> tuple[str, int]:
    path, sep, line = spec.rpartition(":")
    if not sep or not path or not line.isdigit() or int(line) < 1:
        raise SystemExit(f"error: {what} {spec!r} must look like path/to/file.py:LINE")
    return _rel_in_repo(repo, path, what), int(line)


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


def _r_problems(problems: list[dict], indent: str = "  ") -> None:
    for p in problems:
        fix = f"  fix: {p['fix']}" if p.get("fix") else ""
        print(f"{indent}{p.get('at', '/')}: {p.get('msg')}{fix}")


def _r_clarifications(clar: list[dict], indent: str = "  ") -> None:
    for c in clar:
        print(f"{indent}{c['id']}: {c.get('question_user_lang') or c.get('question_en')}")
        for o in c.get("options") or []:
            ev = f"  [{o['evidence']}]" if o.get("evidence") else ""
            print(f"{indent}   - {o.get('value')}: {o.get('label')}{ev}")


def _r_claims(res: dict) -> None:
    snap = res.get("snapshot") or {}
    head = f"analysis {res['analysis_id']}"
    if snap:
        head += (f"  snapshot {snap['id']} commit {(snap.get('commit') or 'no-git')[:10]}"
                 f"{' (dirty)' if snap.get('dirty') else ''}")
    print(head)
    if res.get("understood_as"):
        print(f"understood as: {res['understood_as']}")
    print(f"intents: {', '.join(res.get('intents') or [])}")
    status = res.get("status")
    pc = res.get("plan_check") or {}
    if res.get("plan_id"):
        print(f"plan {res['plan_id']} ({res.get('plan_source')}): {pc.get('status') or status}")
    if status == "invalid_plan":
        print("\nthe plan is invalid; nothing was analysed:")
        _r_problems(res.get("errors") or pc.get("errors") or [])
        print("  next: fix the plan file (`verinoda plan schema`, `verinoda plan check FILE`)")
    elif status == "needs_clarification":
        print("\nclarification needed before analysing (ask the user, record the choice in the plan's answers[] "
              "with its clarification_id, then re-run):")
        _r_clarifications(res.get("clarifications") or pc.get("clarifications") or [])
    elif status == "no_index":
        print("\nno index to analyse: run `verinoda scan` first")
    subs = res.get("subquestions") or []
    if subs:
        print("\nsub-questions:")
        for s in subs:
            n_claims = len(s.get("claim_ids") or [])
            n_unknowns = len(s.get("unknowns") or [])
            print(f"  {s['id']} [{s.get('status') or '?'}] {s.get('intent')}: {s.get('text') or ''}  "
                  f"({n_claims} claim(s){f', {n_unknowns} unknown(s)' if n_unknowns else ''})")
    print()
    for c in res.get("claims") or []:
        print(f"[{c['status']} {c['confidence']:.2f}] {c['text']}  ({c['id']}){_not_challenged_mark(c)}")
        for e in c["evidence"][:3]:
            print(f"      {e}")
        for u in c["uncertainties"][:2]:
            print(f"      ? {u}")
    if res.get("unknowns"):
        print("\nunknown:")
        for u in res["unknowns"]:
            print(f"  - {u['question']}: {u['why']}\n    next: {u['next_step']}")
    if res.get("critique"):
        print("\ncritique:")
        for c in res["critique"]:
            print(f"  {c['claim']}: {c['before']} -> {c['after']}  " + "; ".join((c["fails"] + c["warns"])[:2]))
    u = res.get("usage")
    if u:
        print(f"\nusage: {u['elapsed_s']}s, {u['tool_calls']} tool calls, ~{u['context_tokens_est']} tokens "
              f"({u['token_count_method']}){'; budget: ' + u['exhausted'] if u['exhausted'] else ''}")


def _r_trace(res: dict) -> None:
    print(f"{res['source']} -> {res['target']}  [{res['status']}, mode={res['mode']}]")
    for i, p in enumerate(res["paths"], 1):
        print(f" path {i}:")
        for h in p:
            extra = f" derived_by={h['derived_by']}" if h.get("derived_by") else ""
            kind = f" ({h['kind']})" if h.get("kind") and h.get("kind") != "call" else ""
            print(f"   {h['from']} -{h['relation']}[{h['confidence']}]-> {h['to']}  @{h['at']}{kind}{extra}")
    if res.get("reachability"):
        print(f" reachability: {res['reachability']}")
    if res.get("note"):
        print(f" note: {res['note']}")
    if res.get("undirected_hint"):
        print(" undirected connection only: " + " - ".join(res["undirected_hint"]))
    for side, hints in (res.get("hints") or {}).items():
        print(f" {side} {res.get(side)!r} did not resolve" + ("; did you mean:" if hints else " (no candidates)"))
        for h in hints:
            print(f"   {h['label']}  {h['at']}  (id {h['id']})")
    if res.get("next_step"):
        print(f" next: {res['next_step']}")
    elif res["status"] == "no directed path" and res.get("mode") == "flow":
        print(" next: `--mode any` shows structural relations (imports, uses, containment)")


def _r_claim(res: dict) -> None:
    print(f"{res['id']}  [{res['status']} {res['confidence']:.2f}]  kind={res['kind']}")
    print(f"  {res['text']}")
    print(f"  snapshot {res['snapshot_id']} commit {(res['commit_sha'] or '-')[:10]} "
          f"valid_version {(res['valid_version'] or '-')[:10]}")
    for k in ("supporting", "refuting", "qualifying"):
        for e in res[k]:
            grade = f"  grade={e['grade']}" if e.get("grade") else ""
            print(f"  {k[:-3] if k.endswith('ing') else k}: {e['type']} {e['at']} {e.get('hash') or ''}{grade}")
    for u in res["uncertainties"]:
        print(f"  ? {u}")
    print("  history:")
    for h in res["history"]:
        print(f"    {h['created_at']} {h['from_status']} -> {h['to_status']} ({h['to_confidence']:.2f}) "
              f"{h['actor']}: {h['reason']}")


def _r_challenge(res: dict) -> None:
    print(f"{res['claim']}: {res['before']['status']} {res['before']['confidence']:.2f} -> "
          f"{res['after']['status']} {res['after']['confidence']:.2f}")
    for f in res["findings"]:
        print(f"  [{f['result']}] {f['check']}: {f['detail']}")
    for a in res["alternatives"]:
        print(f"  alternative: {a}")


def _r_verify(r: dict) -> None:
    print(f"{r['claim']}: {r['before']['status']} -> {r['after']['status']} ({r['after']['confidence']:.2f})")
    for c in r.get("source_checks") or []:
        extra = []
        if c.get("status"):
            extra.append(f"status {c['status']}")
        if c.get("moved_to"):
            extra.append(f"moved to {c['moved_to'][0]}-{c['moved_to'][-1]}")
        if c.get("candidate"):
            extra.append(f"candidate {c['candidate'][0]}-{c['candidate'][-1]} (not verification)")
        print(f"  {'ok ' if c['ok'] else 'BAD'} {c['at']}: {c['reason']}"
              + (f"  [{'; '.join(extra)}]" if extra else ""))
    if r.get("experiment"):
        e = r["experiment"]
        print(f"  experiment {e['id']}: {e['outcome']} ({e['isolation']}), logs {e['logs']['stdout']}")
    for f in r.get("unconfirmed_files") or []:
        print(f"  unconfirmed: {f} changed and no re-checkable evidence covers it")
    if r.get("note"):
        print(f"  note: {r['note']}")
    ref = r.get("index_refresh")
    if ref:
        print(f"  index refreshed first: {ref.get('mode')}"
              + (f", {len(ref.get('stale') or [])} claim(s) marked stale" if ref.get("stale") else ""))
        if ref.get("error"):
            print(f"  warning: {ref['error']}" + (f"; hint: {ref['hint']}" if ref.get("hint") else ""))


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
    _r_derived(r)
    if r.get("error"):
        print(f"error: {r['error']}", file=sys.stderr)
        for ln in r.get("index_log_tail") or []:
            print(f"  indexer: {ln}", file=sys.stderr)
    if r.get("hint"):
        print(f"  hint: {r['hint']}", file=sys.stderr)


def _r_derived(r: dict) -> None:
    for name, d in (r.get("derived") or {}).items():
        if isinstance(d, dict) and d.get("error"):
            print(f"  warning: derived {name} not refreshed: {d['error']}")
    if isinstance(r.get("derived"), dict) and r["derived"].get("error"):
        print(f"  warning: derived data not refreshed: {r['derived']['error']}")
    if r.get("pruned_missing_files"):
        print(f"  pruned from the graph (files no longer exist): {', '.join(r['pruned_missing_files'][:5])}")
    dd = r.get("dropped_dangling_references") or {}
    if dd.get("count"):
        print(f"  note: {dd['count']} file(s) named by other files are not in the repository; the graph keeps no "
              f"nodes for them ({', '.join(dd['files'][:3])}{', ...' if dd['count'] > 3 else ''})")


# -- commands -------------------------------------------------------------------

def cmd_doctor(args) -> int:
    from verinoda.doctor import render, run

    res = run(_repo(args))
    _emit(args, res, render)
    return 0 if res["ok"] else 1


def cmd_init(args) -> int:
    from verinoda import workflow

    res = workflow.init(Path(args.path or ".").resolve())
    _emit(args, res, lambda r: print(f"initialised {r['atlas_dir']}"))
    return 0


def cmd_setup(args) -> int:
    from verinoda import setup as setup_mod

    try:
        rep = setup_mod.setup_project(args.path or ".", agents=args.agents, scope=args.scope,
                                      with_mcp=not args.no_mcp, allow_home=args.allow_home,
                                      reference=args.reference)
    except setup_mod.SetupRefused as exc:
        _emit(args, {"ok": False, "error": str(exc)}, lambda r: print(f"error: {r['error']}", file=sys.stderr))
        return 2
    _emit(args, rep, setup_mod.render)
    return 0 if rep["ok"] else 1


def _scan_precise(st, repo: Path, before: dict[str, str], now_files: dict[str, str]) -> dict:
    """``scan --precise``: resolve every call site of the .py files that changed since the previous snapshot."""
    import time
    from collections import Counter

    from verinoda import precise

    ok, why = precise.available()
    if not ok:
        return {"available": False, "reason": why}
    changed = sorted(f for f, h in now_files.items() if f.endswith(".py") and before.get(f) != h)
    t0 = time.monotonic()
    kinds: Counter = Counter()
    sites = cached = 0
    errors: list[str] = []
    for rel in changed:
        try:
            out = precise.resolve_file(repo, rel, store=st)
        except Exception as exc:  # noqa: BLE001 - one bad file never stops the batch; it is reported
            errors.append(f"{rel}: {type(exc).__name__}: {exc}"[:200])
            continue
        sites += len(out)
        cached += sum(1 for r in out if r.get("cached"))
        kinds.update(r.get("kind") or "unresolved" for r in out)
    return {"available": True, "resolver": why,
            "basis": "changed .py files since the previous snapshot" if before else "all .py files (first scan)",
            "files": len(changed), "sites": sites, "cached": cached, "kinds": dict(sorted(kinds.items())),
            "seconds": round(time.monotonic() - t0, 3), **({"errors": errors[:5]} if errors else {})}


def _load_scip(src: Path):
    from verinoda import scip_reader

    if not src.is_file():
        raise SystemExit(f"error: --scip {src} does not exist")
    try:
        return scip_reader.load(src)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: --scip {src} is not a readable SCIP index: {exc}")


def _scan_scip(repo: Path, src: Path, idx) -> dict:
    """Copy a user-supplied SCIP index to .verinoda/index/index.scip (mtime kept) and report freshness."""
    import shutil

    from verinoda import scip_reader
    from verinoda.paths import scip_index_path

    dest = scip_index_path(repo)
    if src.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        # copy2 keeps the modification time: freshness compares it with the indexed files' times
        shutil.copy2(src, dest)
    found = scip_reader.find_index(repo)
    used = found if found is not None else dest
    index = idx if used.resolve() == dest.resolve() else scip_reader.load(used)
    fresh = scip_reader.document_freshness(repo, used, index)
    n, k = len(fresh), sum(1 for v in fresh.values() if v)
    tool = f"{idx.tool_name or 'unknown'} {idx.tool_version or ''}".strip()
    out = {"index": str(dest), "source": str(src), "tool": tool,
           "documents": n, "fresh": k, "fresh_share": round(k / n, 3) if n else None,
           "stale": sorted(p for p, v in fresh.items() if not v)[:10]}
    if used.resolve() != dest.resolve():
        out["warning"] = (f"{used} takes precedence (the repository root is read first); remove it to use "
                          "the copied index")
    return out


def cmd_scan(args) -> int:
    from verinoda import workflow

    if not (args.repo or args.path):  # never the working directory by accident: scanning is heavy
        raise SystemExit("verinoda scan: give the project folder (PATH or --repo), for example `verinoda scan .`")
    repo = Path(args.repo or args.path).resolve()
    scip_src = Path(args.scip).resolve() if args.scip else None
    scip_idx = _load_scip(scip_src) if scip_src else None   # fail fast, before the (long) scan
    workflow.init(repo)
    st = _store(repo)
    prev = st.latest_snapshot()
    before = st.snapshot_files(prev["id"]) if prev else {}
    res = workflow.scan(st, repo, force=args.force)
    if args.precise:
        snap = res.get("snapshot") or {}
        if snap.get("id") and not res.get("error"):
            now_files = st.snapshot_files(snap["id"])
        else:
            from verinoda.snapshot import current_state

            now_files = current_state(repo, store=st)["files"]
        res["precise"] = _scan_precise(st, repo, before, now_files)
    if scip_src is not None:
        res["scip"] = _scan_scip(repo, scip_src, scip_idx)

    def render(r):
        g = r.get("graph") or {}
        snap = r.get("snapshot") or {}
        if r.get("error"):
            print(f"scan of {repo.name} did not rebuild the index", file=sys.stderr)
        else:
            print(f"indexed {repo.name}: {g.get('nodes')} nodes, {g.get('edges')} edges in "
                  f"{r.get('index_seconds')}s")
        if snap:
            print(f"snapshot {snap['id']} commit {(snap.get('commit_sha') or 'no-git')[:10]} "
                  f"files {snap.get('file_count')}; {len(r.get('stale') or [])} claim(s) marked stale")
        for w in r.get("warnings") or []:
            print(f"  warning: {w}")
        _r_derived(r)
        p = r.get("precise")
        if p is not None:
            if not p["available"]:
                print(f"  precise: not run - {p['reason']}")
            else:
                print(f"  precise ({p['resolver']}): {p['sites']} call site(s) in {p['files']} file(s) "
                      f"[{p['basis']}], {p['cached']} cached, {p['seconds']}s: "
                      + (", ".join(f"{k} {v}" for k, v in p["kinds"].items()) or "nothing to resolve"))
                for e in p.get("errors") or []:
                    print(f"    error: {e}")
        s = r.get("scip")
        if s is not None:
            print(f"  scip: {s['index']} ({s['tool']}): {s['fresh']}/{s['documents']} documents fresh"
                  + (f"; stale: {', '.join(s['stale'][:5])}" if s["stale"] else ""))
            if s.get("warning"):
                print(f"  warning: {s['warning']}")
        if r.get("error"):
            print(f"error: {r['error']}", file=sys.stderr)
            if r.get("hint"):
                print(f"  hint: {r['hint']}", file=sys.stderr)
    _emit(args, res, render)
    return 1 if res.get("error") else 0


def cmd_update(args) -> int:
    from verinoda import workflow

    given = args.repo or args.path
    repo = Path(given).resolve() if given else find_repo_root()
    st = _store(repo, create=True)
    res = workflow.update(st, repo)
    _emit(args, res, _r_update)
    return 1 if res.get("error") else 0


def _port(value: str) -> int:
    n = int(value)
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"{value} is not a port (0-65535; 0 picks a free one)")
    return n


def cmd_ui(args) -> int:
    from verinoda.ui.server import serve

    given = args.repo or args.path
    repo = Path(given).resolve() if given else find_repo_root()
    try:
        serve(repo, port=args.port, open_browser=not args.no_browser)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:  # the port is taken
        print(f"error: cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_map(args) -> int:
    from verinoda import architecture_map as am
    from verinoda import index

    repo = Path(getattr(args, "repo", None) or args.path).resolve()
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
        _write(_dump(res))
        return 0
    for name, v in res.items():
        print(f"== {name} ==  ({v['coverage']['method']})")
        for lim in v["coverage"].get("limits", []):
            print(f"   limit: {lim}")
        body = {k: val for k, val in v.items() if k not in ("view", "coverage")}
        text = _dump(body)
        lines = text.splitlines()
        _write("\n".join(lines[: args.max_lines]))
        if len(lines) > args.max_lines:
            print(f"   ... {len(lines) - args.max_lines} more lines (use --json or --view)")
    return 0


def cmd_query(args) -> int:
    from verinoda import index, retrieval

    repo = _repo(args)
    _need_graph(repo)
    g = index.load(repo)
    res = retrieval.retrieve(g, args.question, retrieval.Budget(max_items=args.max_items, max_chars=args.max_chars))
    if args.json:
        _write(_dump(res))
    else:  # the skeleton-first plain text a model reads (docs/DESIGN.md D20)
        _write(retrieval.render_text(res, budget_chars=args.max_chars))
    return 0


def cmd_trace(args) -> int:
    from verinoda import index, retrieval

    repo = _repo(args)
    _need_graph(repo)
    res = retrieval.trace(index.load(repo), args.source, args.target, mode=args.mode)
    _emit(args, res, _r_trace)
    return 0 if res["status"] == "found" else 2


# -- question plans (docs/DESIGN.md D1-D9) ----------------------------------------

def _plan_file(repo: Path, arg: str) -> Path:
    """An existing plan file: as given, relative to the repository, or under .verinoda/plans/."""
    from verinoda.paths import plans_dir

    if arg.lstrip().startswith(("{", "[")):
        raise SystemExit("error: pass the plan as a FILE, never as JSON on the command line (Windows PowerShell "
                         "5.1 mangles quoted JSON); write it under .verinoda/plans/ - `verinoda plan draft` "
                         "creates one")
    p = Path(arg)
    cands = [p] if p.is_absolute() else [Path.cwd() / p, repo / p, plans_dir(repo) / p]
    for c in cands:
        if c.is_file():
            return c.resolve()
    raise SystemExit(f"error: plan file {arg} not found (looked in: {', '.join(str(c) for c in cands)})")


def _next_plan_file(repo: Path) -> Path:
    from verinoda.paths import plans_dir

    d = plans_dir(repo)
    nums = [int(m.group(1)) for p in (d.glob("plan-*.json") if d.is_dir() else [])
            if (m := re.fullmatch(r"plan-(\d+)\.json", p.name))]
    return d / f"plan-{max(nums, default=0) + 1:03d}.json"


def _write_json_file(path: Path, obj) -> None:
    """UTF-8, LF line endings, whatever the platform (newline='' semantics)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _display_path(p: Path) -> str:
    try:
        return p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(p)


def _r_plan_draft(r: dict) -> None:
    plan = r["plan"]
    print(f"wrote {r['file']} (rule-based draft: check the reading, split compound questions, gloss domain "
          f"words, copy versions exactly as written; then `verinoda plan check {r['file']}`)")
    print(f"  language: {plan.get('language')}; restated goal: "
          f"{plan.get('restated_goal_user_lang') or plan.get('restated_goal')}")
    for sq in plan.get("sub_questions") or []:
        print(f"  {sq['id']} [{sq.get('intent')}] {sq.get('text')}"
              + (f"  mentions: {', '.join(sq['mentions'])}" if sq.get("mentions") else ""))
    for m in plan.get("mentions") or []:
        print(f"  {m['id']} {m.get('text')!r}" + (f" -> {m['gloss_en']}" if m.get("gloss_en") else "")
              + (f"  candidates: {', '.join(m['candidates'][:3])}" if m.get("candidates") else ""))
    for ref in plan.get("references") or []:
        ver = (ref.get("version") or {}).get("spec")
        print(f"  {ref['id']} reference {ref.get('text')!r}" + (f" @ {ver}" if ver else ""))


def _r_plan_check(r: dict) -> None:
    print(f"plan check: {r['status']}  (plan {r.get('plan_id') or '-'} from {r['file']})")
    if r.get("errors"):
        print("  errors:")
        _r_problems(r["errors"], "    ")
    for w in r.get("warnings") or []:
        print(f"  warning [{w['code']}]: {w['msg']}")
    for lk in r.get("links") or []:
        where = f" {lk['label']} at {lk['at']} ({lk.get('tier')}, {lk.get('score')})" if lk.get("at") else ""
        extra = f"; rejected candidates: {', '.join(lk['rejected'])}" if lk.get("rejected") else ""
        print(f"  {lk['mention']} {lk['text']!r}: {lk['status']}{where}{extra}")
    for ref in r.get("references") or []:
        ver = f" @ {ref['version_used']}" if ref.get("version_used") else ""
        status = ref.get("status") or "not checked offline"
        print(f"  reference {ref.get('reference')} {ref.get('text')!r}{ver}: {status}"
              + (f" ({ref['evidence']})" if ref.get("evidence") else "")
              + (f"; next: {ref['next_step']}" if ref.get("next_step") else ""))
    if r.get("clarifications"):
        print("  clarifications (ask the user; record answers[] with each clarification_id):")
        _r_clarifications(r["clarifications"], "    ")
    for u in r.get("unknowns") or []:
        print(f"  unknown: {u['question']} - {u['why']}\n    next: {u['next_step']}")
    if r["status"] == "ready":
        print(f"  next: verinoda analyze --plan {r['file']}")


def cmd_plan(args) -> int:
    from verinoda import question_plan as qp

    if args.plan_cmd == "schema":
        _write(_dump(qp.schema()))
        return 0
    repo = _repo(args)
    if args.plan_cmd == "audit":
        from verinoda import analysis

        res = analysis.audit(_store(repo), repo, args.analysis_id, refresh=not args.no_refresh)

        def render(r):
            ref = r.get("refreshed") or {}
            print(f"audit {r['analysis_id']} (plan {r.get('plan_id') or '-'}): {r.get('question')}")
            if ref:
                print(f"  index refresh: {ref.get('mode') or '-'}"
                      + (f", {ref['stale']} claim(s) marked stale" if ref.get("stale") else "")
                      + (f"; error: {ref['error']}" if ref.get("error") else ""))
            for s in r["subquestions"]:
                stale = f"  stale claims: {', '.join(s['stale_claims'])}" if s["stale_claims"] else ""
                print(f"  {s['id']} [{s.get('intent')}] {s.get('was')} -> {s['now']}{stale}")
            print(f"  changed: {', '.join(r['changed']) if r['changed'] else 'none'}")
        _emit(args, res, render)
        return 0

    from verinoda import index, lexicon

    _need_graph(repo)
    g = index.load(repo)
    lex = lexicon.load(repo)
    if args.plan_cmd == "draft":
        plan = qp.draft(args.message, g, lex)
        if not args.out:
            out = _next_plan_file(repo)
        elif Path(args.out).name == args.out:  # a bare file name: under .verinoda/plans/, like `plan check`
            from verinoda.paths import plans_dir

            out = plans_dir(repo) / args.out
        else:
            out = Path(args.out).resolve()
        if out.suffix.lower() != ".json":
            raise SystemExit(f"error: --out {args.out} must be a .json file")
        _write_json_file(out, plan)
        _emit(args, {"file": _display_path(out), "plan": plan}, _r_plan_draft)
        return 0
    # check
    path = _plan_file(repo, args.file)
    plan, problems = qp.parse(path)
    st = _store(repo)
    snap = st.latest_snapshot()
    if plan is None:
        check = {"schema": qp.CHECK_SCHEMA_ID, "status": "invalid", "errors": problems, "warnings": [],
                 "links": [], "references": [], "clarifications": [], "unknowns": []}
        plan_id = qp.store_plan(st, {"unparsed": path.read_text(encoding="utf-8-sig", errors="replace")[:20000]},
                                check, "host", snapshot_id=(snap or {}).get("id"))
    else:
        check = qp.check(plan, g, repo, lex, source="host")
        plan_id = qp.store_plan(st, plan, check, "host", snapshot_id=(snap or {}).get("id"))
    res = {"plan_id": plan_id, "file": _display_path(path), **qp.compact_check(check),
           "unknowns": check.get("unknowns") or []}
    _emit(args, res, _r_plan_check)
    return PLAN_EXIT.get(check["status"], 1)


def cmd_analyze(args) -> int:
    from verinoda import analysis

    repo = _repo(args)
    _need_graph(repo)
    plan = _plan_file(repo, args.plan) if args.plan else None
    question = args.question or ""
    if plan is None and not question.strip():
        raise SystemExit("error: give a question, or a plan file with --plan FILE")
    b = analysis.Budget(seconds=args.budget_seconds, tool_calls=args.budget_calls,
                        context_tokens=args.budget_tokens)
    res = analysis.analyze(_store(repo), repo, question, plan=plan, budget=b, run_tests=args.run_tests,
                           challenge=not args.no_challenge, observe=args.observe)
    _emit(args, res, _r_claims)
    return PLAN_EXIT.get(res.get("status"), 0)


# -- claims -----------------------------------------------------------------------

def cmd_claim(args) -> int:
    from verinoda.claims import Claims

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
        _emit(args, res, lambda rs: [print(f"{r['id']} [{r['status']} {r['confidence']:.2f}] {r['text'][:100]}")
                                     for r in rs])
    elif args.claim_cmd == "add":
        from verinoda import evidence as evmod
        from verinoda import workflow

        sources = []
        for spec in args.source or []:
            path, _, rng = spec.rpartition(":")
            a, _, b = rng.partition("-")
            if not path or not a.isdigit() or (b and not b.isdigit()):
                raise SystemExit(f"error: --source {spec!r} must look like path/to/file.py:START[-END]")
            sources.append((spec, _rel_in_repo(repo, path, "--source"), int(a), int(b or a)))
        # Bind to a snapshot of the tree as it is NOW (the evidence is read from it): refresh the
        # index first when the working tree moved on (acceptance criterion 7).
        snap, refresh = workflow._current_snapshot(st, repo)
        if refresh and refresh.get("error"):
            raise SystemExit(f"error: the index could not be refreshed ({refresh['error']}); a claim added now "
                             "would be bound to a snapshot that does not describe the current tree"
                             + (f"\n  hint: {refresh['hint']}" if refresh.get("hint") else ""))
        if snap is None:
            raise SystemExit(f"error: {repo} has no snapshot - run `verinoda scan {repo}` first")
        evs = []
        for spec, path, a, b in sources:
            ev = evmod.source_evidence(repo, path, a, b, commit=snap["commit_sha"])
            if ev is None:
                raise SystemExit(f"error: {spec} does not exist")
            evs.append((ev, "refutes" if args.refutes else "supports"))
        kind = args.kind or "general"
        # The claim text is the user's own words: kind predicates (location, relation, config)
        # check the cited code, but the text must also be about what the lines say
        # (entail.assess caps the predicate grade by term coverage; audit 2026-09-23).
        cspec: dict = {"free_text": True}
        subjects = list(dict.fromkeys(p for _, p, _, _ in sources))
        if args.symbol:
            cspec["symbol"] = args.symbol
            if kind == "relation":
                cspec["target_label"] = args.symbol
                if "::" in args.symbol:  # path::symbol names the target file too
                    subjects = subjects[:1] + [args.symbol]
            elif kind == "config":
                cspec["env"] = args.symbol
        if kind == "relation" and sources:
            cspec["at"] = f"{sources[0][1]}:{sources[0][2]}"
        c = Claims(st, repo).create(args.text, project=snap["project"], snapshot=snap, status=args.status,
                                    evidence=evs, subjects=subjects, kind=kind, spec=cspec or None,
                                    actor="user")
        res = Claims(st, repo).show(c["id"])
        if refresh and not args.json:
            print(f"(index refreshed first: {refresh.get('mode')}"
                  + (f", {len(refresh.get('stale') or [])} claim(s) marked stale" if refresh.get("stale") else "")
                  + ")", file=sys.stderr)
        _emit(args, res, _r_claim)
    return 0


def cmd_verify(args) -> int:
    from verinoda import workflow

    repo = _repo(args)
    res = workflow.verify(_store(repo), repo, args.id, run=args.run)
    _emit(args, res, _r_verify)
    return 0


def cmd_challenge(args) -> int:
    from verinoda import critique, index

    from verinoda.paths import graph_path

    repo = _repo(args)
    g = index.load(repo) if graph_path(repo).exists() else None
    res = critique.challenge(_store(repo), repo, args.id, graph=g)
    _emit(args, res, _r_challenge)
    return 0


# -- references and research (docs/DESIGN.md D10-D16) ----------------------------------

def cmd_resolve(args) -> int:
    from verinoda import references
    from verinoda.paths import network_mode

    repo = _repo(args)
    network = args.network or network_mode(repo)
    st = _store(repo, create=True)
    try:
        res = references.resolve(st, repo, args.text, explicit=args.reference or [], network=network,
                                 local_intent=True if args.local_intent else None)
    finally:
        st.close()
    if args.json:
        _write(_dump(references.compact(res)))
    else:
        _write(references.render_text(res))
    return 0 if res.get("status") == "complete" else 3


def _resolved_reference(st, resolution_id: str, reference_id: str) -> dict:
    row = st.get("reference_resolutions", resolution_id)
    if row is None:
        raise KeyError(f"no reference resolution {resolution_id} (run `verinoda resolve \"<text>\"` first)")
    result = row.get("result") or {}
    refs = result.get("references") or []
    ref = next((r for r in refs if r.get("id") == reference_id), None)
    if ref is None:
        raise KeyError(f"resolution {resolution_id} has no reference {reference_id} "
                       f"(it has: {', '.join(r.get('id', '?') for r in refs) or 'none'})")
    return ref


def cmd_research(args) -> int:
    from verinoda import research

    repo = _repo(args)
    st = _store(repo, create=True)
    if args.resolution or args.reference_id:
        if not (args.resolution and args.reference_id):
            raise SystemExit("error: --resolution and --reference-id go together (e.g. --resolution rrs_x "
                             "--reference-id r1)")
        if args.reference:
            raise SystemExit("error: give either a reference or --resolution/--reference-id, not both")
        from verinoda import feedback

        ref = _resolved_reference(st, args.resolution, args.reference_id)
        target, pin = (feedback._research_target(ref) if ref.get("status") in ("pinned", "pinned_floating")
                       else (None, None))
        if not target:
            unres = (ref.get("unresolved") or [{}])[0]
            out = {"status": "unresolved", "resolution_id": args.resolution, "reference_id": args.reference_id,
                   "class": ref.get("class"), "reference_status": ref.get("status"),
                   "why": unres.get("why") or (f"reference {args.reference_id} is {ref.get('status')}; it has no "
                                                f"pin that can be researched (class {ref.get('class')})"),
                   "next_step": unres.get("next_step") or "resolve the reference again with the version the user "
                                                          "meant (`verinoda resolve \"<text>\" --network on`)"}
            _emit(args, out, lambda r: print(f"research {r['reference_id']} of {r['resolution_id']}: unknown - "
                                             f"{r['why']}\n  next: {r['next_step']}"))
            return 3
        pin = {**pin, "resolution_id": args.resolution, "reference_id": args.reference_id}
        if feedback._is_dependency(ref) and ref.get("class") not in getattr(feedback, "_DOC_CLASSES", ()):
            pin["source_type"] = "dependency_source"   # the version this project locks: rank 4 evidence
        full = research.research_full(st, repo, target, topic=args.topic, kind=args.kind, pin=pin,
                                      verify_source=not args.no_verify_source)
    else:
        if not args.reference:
            raise SystemExit("error: give a reference (repository URL/path, owner/repo, package, document URL) "
                             "or --resolution ID --reference-id rN")
        full = research.research_full(st, repo, args.reference, ref=args.ref, topic=args.topic, kind=args.kind,
                                      verify_source=not args.no_verify_source)
    res = research.compact(full)
    _emit(args, res, research.render)
    return 0 if res.get("status") == "ok" else 1


def cmd_compare(args) -> int:
    from verinoda import research

    local = Path(args.local).resolve()
    res = research.compare(_store(local, create=True), local, args.reference, ref=args.ref, topic=args.topic)
    _emit(args, res, research.render_compare)
    return 0 if res.get("status") == "ok" else 1


def cmd_feedback(args) -> int:
    from verinoda import feedback

    repo = _repo(args)
    st = _store(repo)
    if args.fb_cmd == "add":
        refs = args.reference or []
        res = feedback.add(st, repo, args.text, claim_id=args.claim, reference=refs[0] if refs else None,
                           ref=args.ref, correction=args.correction, expect_pattern=args.expect_pattern,
                           expect_in=args.expect_in, references=refs[1:] or None)
        if args.process:
            res = feedback.process(st, repo, res["id"], topic=args.topic, network=args.network)
    elif args.fb_cmd == "process":
        res = feedback.process(st, repo, args.id, topic=args.topic, network=args.network)
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


# -- experiments, runtime observation, precise resolution ---------------------------------

def cmd_experiment(args) -> int:
    from verinoda import experiments
    from verinoda.paths import load_config

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
        allow = load_config(repo)["experiments"]["process_isolation_allowlist"]
        out = {"status": "refused", "reason": str(exc),
               "limits": ["without docker/podman only allowlisted test runners run, and only with process "
                          "isolation (no network or filesystem confinement)",
                          f"allowlist (config experiments.process_isolation_allowlist): {', '.join(allow)}"],
               "next_step": "run the tests through an allowlisted runner with paths inside the repository, or "
                            "install docker/podman for container isolation"}

        def render_refused(r):
            print(f"refused: {r['reason']}")
            for lim in r["limits"]:
                print(f"  limit: {lim}")
            print(f"  next: {r['next_step']}")
        _emit(args, out, render_refused)
        return 3

    def render(r):
        s = r["summary"]
        print(f"{r['id']}: {r['outcome']} in {r['duration_s']}s  isolation={r['isolation']} "
              f"(matches expectation: {r['matches_expectation']})")
        for ln in s["summary_lines"] or s["tail"][-3:]:
            print(f"  {ln}")
        for ln in s["relevant_lines"][:8]:
            print(f"  ! {ln}")
        if r.get("inconclusive_reason"):
            print(f"  inconclusive: {r['inconclusive_reason']}")
        if r.get("next_step"):
            print(f"  next: {r['next_step']}")
        print(f"  evidence {r['evidence_id']}; full logs: {r['logs']['stdout']}")
        print("  isolation guarantees: " + ", ".join(k for k, v in r["guarantees"].items() if v)
              + "; NOT: " + ", ".join(k for k, v in r["guarantees"].items() if not v))
        for lim in r.get("limits") or []:
            print(f"  limit: {lim}")
    _emit(args, res, render)
    return 0


def _observe_summary(res: dict, g, symbols: list[str], selected: list[str] | None) -> dict:
    """The capped part of ``runtime.trace.observe``: never the edges or evidence lists."""
    from collections import Counter

    target_files = set()
    unresolved = []
    for s in symbols:
        nid = s if s in g.G else g.resolve(s)[0]
        if nid and g.file(nid):
            target_files.add(g.file(nid))
        else:
            unresolved.append(s)
    boundary = [{"site": b["site"], "callee": b["callee"], "tests": b.get("tests", [])[:3]}
                for b in res.get("boundary") or [] if b["site"].rpartition(":")[0] in target_files]
    comp = {k: v for k, v in (res.get("completeness") or {}).items() if v not in (None, [], {})}
    traced = res.get("tracer") not in (None, "off")
    got = res.get("target_reach") or {}
    reach = {}
    for s in symbols:  # keyed by the symbol as the user wrote it
        nid = s if s in g.G else g.resolve(s)[0]
        tests = got.get(nid or s)
        if tests is None:
            continue
        reach[s] = {"node": nid, "tests": tests[:OBSERVE_LIST_CAP], "n": len(tests),
                    # "not reached" needs a complete trace that actually recorded calls
                    "observed": traced and bool(res.get("complete"))}
    out = {"run_id": res.get("run_id"), "experiment_id": res.get("experiment_id"),
           "complete": bool(res.get("complete")),
           "scope": res.get("scope"), "tracer": res.get("tracer"), "commit": res.get("commit"),
           "snapshot_id": res.get("snapshot_id"), "outcome": res.get("outcome"), "completeness": comp,
           "test_outcomes": dict(Counter((res.get("tests") or {}).values())),
           "target_reach": reach,
           "boundary_for_targets": boundary[:OBSERVE_LIST_CAP], "boundary_for_targets_total": len(boundary),
           "edges_total": res.get("edges_total"), "boundary_total": res.get("boundary_total"),
           "evidence_records": len(res.get("evidence") or []),
           "limits": res.get("limits") or [],
           "cost": {**{k: v for k, v in (res.get("cost") or {}).items() if k in ("cpu_s", "wall_s")},
                    "duration_s": res.get("duration_s")}}
    if selected is not None:
        out["tests_selected"] = selected[:OBSERVE_LIST_CAP]
        out["tests_selected_total"] = len(selected)
    if unresolved:
        out["unresolved_targets"] = unresolved
    for k in ("error", "next_step", "truncated_ids"):
        if res.get(k):
            out[k] = res[k]
    if res.get("logs"):
        out["logs"] = res["logs"].get("stderr")
    return {k: v for k, v in out.items() if v is not None}


def _r_observe(r: dict) -> None:
    if r.get("status") == "unknown":
        print(f"unknown: {r['question']} - {r['why']}\n  next: {r['next_step']}")
        return
    outcomes = ", ".join(f"{v} {k}" for k, v in sorted(r.get("test_outcomes", {}).items())) or "no tests recorded"
    print(f"observe run {r.get('run_id')} (experiment {r.get('experiment_id')}): "
          f"{'complete' if r.get('complete') else 'INCOMPLETE'}, tracer {r.get('tracer') or '-'}, "
          f"{r.get('scope')}; {outcomes}")
    if r.get("tests_selected"):
        print(f"  tests selected by static reach/name ({r['tests_selected_total']}): "
              + ", ".join(r["tests_selected"]))
    comp = r.get("completeness") or {}
    if comp.get("breach") or comp.get("degraded_after"):
        print(f"  budget breach: {comp.get('breach')} after {comp.get('degraded_after')}")
    for t, v in (r.get("target_reach") or {}).items():
        if v["n"]:
            print(f"  {t}: reached by {v['n']} test(s): {', '.join(v['tests'])}")
        elif v.get("observed"):
            print(f"  {t}: not reached by the tests of this run (run-scoped, not 'never')")
        else:
            print(f"  {t}: unknown - " + ("the tracer was off (outcomes and timing only)" if r.get("tracer") == "off"
                                          else "the trace is incomplete, so absence is inconclusive"))
    for b in r.get("boundary_for_targets") or []:
        print(f"  boundary call {b['site']} -> {b['callee']}")
    for t in r.get("unresolved_targets") or []:
        print(f"  target {t!r} is not a graph symbol (matched by name only)")
    print(f"  recorded: {r.get('edges_total')} in-repo edge(s), {r.get('boundary_total')} boundary call site(s), "
          f"{r.get('evidence_records')} evidence record(s) (not printed; `--json` for the summary)")
    cost = r.get("cost") or {}
    print(f"  cost: cpu {cost.get('cpu_s')} s, wall {cost.get('wall_s')} s, run {cost.get('duration_s')} s")
    for lim in r.get("limits") or []:
        print(f"  limit: {lim}")
    if r.get("error"):
        print(f"  error: {r['error']}")
    if r.get("next_step"):
        print(f"  next: {r['next_step']}")


def cmd_observe(args) -> int:
    from verinoda import index
    from verinoda.runtime import trace as rt

    repo = _repo(args)
    _need_graph(repo)
    st = _store(repo)
    g = index.load(repo)
    symbols = list(dict.fromkeys(args.for_symbols or []))
    terms = list(dict.fromkeys(args.terms or []))
    ids = list(args.test_ids or [])
    selected = None
    if not ids and (symbols or terms):  # select; with neither, the whole suite runs
        ids = rt.select_tests(g, symbols, terms=terms)
        selected = ids
        if not ids:
            out = {"status": "unknown", "question": f"which tests run {', '.join(symbols or terms)}?",
                   "why": "no test statically reaches these symbols and no test name contains them or a --terms "
                          "word; nothing was run",
                   "next_step": "pass pytest node ids (or a test directory) explicitly: `verinoda observe tests/"
                                + (" --for " + " ".join(symbols) if symbols else "") + "`"}
            _emit(args, out, _r_observe)
            return 3
    res = rt.observe(st, repo, ids, timeout=args.timeout, snapshot=st.latest_snapshot(), graph=g,
                     targets=symbols, mode=args.mode)
    summary = _observe_summary(res, g, symbols, selected)
    _emit(args, summary, _r_observe)
    # 3: the run did not establish what was asked (incomplete trace, or reach asked with the tracer off)
    unobserved = any(not v["n"] and not v["observed"] for v in summary.get("target_reach", {}).values())
    return 0 if res.get("complete") and not unobserved else 3


def _r_resolution(r: dict) -> None:
    if r.get("answer") is None and "kind" not in r:
        print(f"no precise answer for {r['site']} {r['target']}: {r['why']}\n  resolver: {r['resolver']}"
              f"\n  next: {r['next_step']}")
        return
    verdict = f"  verdict: {r['verdict']}" if r.get("verdict") else ""
    print(f"{r['path']}:{r['line']} {r.get('token')} -> {r['kind']} ({r.get('tool')}){verdict}"
          + ("  [cached]" if r.get("cached") else ""))
    for t in (r.get("targets") or [])[:5]:
        where = f"{t.get('path')}:{t.get('line')}" if t.get("path") else "(outside the repository)"
        print(f"  target: {where} {t.get('qualname') or t.get('name')} ({t.get('type')})")
    if r.get("reason"):
        print(f"  reason: {r['reason']}")
    if r.get("uncertainty"):
        print(f"  ? {r['uncertainty']}")


def cmd_resolve_call(args) -> int:
    from verinoda import precise
    from verinoda.paths import db_path

    repo = _repo(args)
    path, line = _path_line(repo, args.site, "site")
    tpath, tline = _path_line(repo, args.target_at, "--target") if args.target_at else (None, None)
    st = _store(repo) if db_path(repo).is_file() else None   # the answer cache; optional
    res = precise.resolve_call(repo, path, line, args.target, store=st, target_path=tpath, target_line=tline)
    if res is None:
        ok, why = precise.available()
        if not path.endswith((".py", ".pyi")):
            reason = "no fresh index.scip covers this file (SCIP answers non-Python files)"
            nxt = "generate a SCIP index for the language and run `verinoda scan <path> --scip FILE`"
        elif not ok:
            reason = why
            nxt = "pip install 'verinoda[precise]'"
        else:
            reason = f"{path} cannot be read or is outside the repository"
            nxt = "check the path (repository-relative) and line"
        out = {"answer": None, "status": "no precise answer", "site": f"{path}:{line}", "target": args.target,
               "resolver": why, "why": reason, "next_step": nxt}
        _emit(args, out, _r_resolution)
        return 3
    _emit(args, res, _r_resolution)
    return 0


def cmd_memory(args) -> int:
    from verinoda.memory import Memory

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
    _write(_dump(res))
    return 0


def cmd_install(args) -> int:
    from verinoda import agents

    res = agents.install(args.agent, args.scope, project_dir=Path(args.project_dir or ".").resolve(),
                         with_mcp=not args.no_mcp, dry_run=args.dry_run)
    _emit(args, res, agents.render)
    return 0 if res.get("ok", True) else 1


def cmd_uninstall(args) -> int:
    from verinoda import agents

    res = agents.uninstall(args.agent, args.scope, project_dir=Path(args.project_dir or ".").resolve(),
                           dry_run=args.dry_run)
    _emit(args, res, agents.render)
    return 0 if res.get("ok", True) else 1


def cmd_mcp(args) -> int:
    from verinoda.mcp.server import default_repo, serve

    serve(_repo(args) if args.repo else default_repo(Path.cwd()))
    return 0


# Upstream subcommands that write Graphify-branded skills, hooks, merge drivers or
# agent configs into the user's home or project (CLAUDE.md, AGENTS.md, .claude/,
# .codex/, git hooks, .gitattributes, ...). Run through `verinoda index` they would
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
# Upstream subcommands (and the `--global` flag of extract/update) that write under
# ~/.graphify - outside the project and outside .verinoda: `clone` caches checkouts in
# ~/.graphify/repos, `provider` keeps ~/.graphify/providers.json, `global` and `--global`
# merge graphs into ~/.graphify's global graph. Verinoda keeps reference checkouts under
# .verinoda/research (`verinoda research`), so these are refused as well.
UPSTREAM_HOME_WRITERS = frozenset({"clone", "provider", "global"})
UPSTREAM_HOME_FLAGS = ("--global",)


def _blocked_index_commands() -> frozenset:
    try:
        from verinoda.project_index.install import _CLI_INSTALL_COMMANDS
    except Exception:  # pragma: no cover - upstream layout changed; the static list still applies
        return UPSTREAM_BLOCKED
    return UPSTREAM_BLOCKED | frozenset(_CLI_INSTALL_COMMANDS)


def cmd_index(args) -> int:
    """Pass-through to the Graphify-derived CLI (advanced/upstream features).

    Installer-type subcommands are refused (exit 2): see ``UPSTREAM_BLOCKED``;
    so are commands that write under ``~/.graphify``: see ``UPSTREAM_HOME_WRITERS``.
    """
    rest = args.rest[1:] if args.rest and args.rest[0] == "--" else args.rest
    if rest and rest[0] in _blocked_index_commands():
        print(f"error: `verinoda index {rest[0]}` is blocked: this upstream Graphify command writes "
              "Graphify-branded skills, hooks, merge drivers or agent configs and could overwrite or remove "
              "a real Graphify install.\n"
              "  Use `verinoda install --agent claude|codex --scope project|user` for the Verinoda skill + MCP "
              "(and `verinoda uninstall` to remove exactly what it added).", file=sys.stderr)
        return 2
    flag = next((a for a in rest if a.split("=", 1)[0] in UPSTREAM_HOME_FLAGS), None)
    if rest and (rest[0] in UPSTREAM_HOME_WRITERS or flag):
        what = f"{rest[0]} {flag}" if flag and rest[0] not in UPSTREAM_HOME_WRITERS else rest[0]
        print(f"error: `verinoda index {what}` is blocked: this upstream Graphify command writes under "
              "~/.graphify (clone cache ~/.graphify/repos, provider registry ~/.graphify/providers.json or the "
              "global graph), outside this project and outside .verinoda/; Verinoda never writes to your home "
              "directory implicitly.\n"
              "  Use `verinoda research <url|owner/repo> [--ref R]` for a pinned checkout under "
              ".verinoda/research, and `verinoda resolve \"<text>\"` to pin the version you mean first.",
              file=sys.stderr)
        return 2
    from verinoda.project_index.__main__ import main as upstream_main

    sys.argv = ["verinoda index", *rest]
    upstream_main()
    return 0


# -- benchmarks ---------------------------------------------------------------------

def _bench_out(path: str | None, res: dict) -> None:
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((json.dumps(res, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8"))


def _bench_compact(res: dict, out: str | None) -> dict:
    """A harness result without its per-case rows (they go to --out)."""
    small = {k: v for k, v in res.items() if k != "rows"}
    if "rows" in res:
        small["rows_omitted"] = len(res["rows"])
        if out:
            small["full_result"] = out
    return small


def _render_flat(res: dict, max_lines: int = 40) -> None:
    lines = []
    for k, v in res.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if not isinstance(v2, (dict, list)):
                    lines.append(f"  {k}.{k2}: {v2}")
                elif isinstance(v2, dict):
                    scalars = [(a, b) for a, b in v2.items() if not isinstance(b, (dict, list))]
                    if scalars:
                        lines.append(f"  {k}.{k2}: " + ", ".join(f"{a}={b}" for a, b in scalars))
        elif isinstance(v, list):
            lines.append(f"  {k}: {len(v)} item(s)")
        else:
            lines.append(f"  {k}: {v}")
    for ln in lines[:max_lines]:
        print(ln)
    if len(lines) > max_lines:
        print(f"  ... {len(lines) - max_lines} more (use --json or --out)")


def cmd_benchmark(args) -> int:
    if args.bench_cmd == "sanitize":
        from verinoda.benchmark.sanitize import sanitize_result_file

        res = [sanitize_result_file(Path(f)) for f in args.results]
        _emit(args, res, lambda rs: [print(f"{r['file']}: json changed {r['json_changed']}, "
                                           f"{r['contexts_changed']} context(s) changed; placeholders: "
                                           f"{', '.join(r['placeholders']) or '-'}") for r in rs])
        return 0
    if args.bench_cmd == "staleness":
        from verinoda.benchmark import staleness

        if args.stale_cmd == "replay":
            def progress(i: int, n: int, sha: str) -> None:
                if i % 10 == 0 or i == n:
                    print(f"[staleness] {i}/{n} {sha[:10]}", file=sys.stderr, flush=True)

            res = staleness.replay(Path(args.repo).resolve(), commits=args.commits, pathspec=args.pathspec,
                                   cap_per_kind=args.cap or None, progress=progress)
        else:
            res = staleness.run_mutation_suite()
        _bench_out(args.out, res)
        _emit(args, _bench_compact(res, args.out),
              lambda r: (print(f"staleness {args.stale_cmd}" + (f" (full result: {args.out})" if args.out else "")),
                         _render_flat(r)))
        return 0 if res.get("recall_ok", res.get("all_ok", True)) and res.get("silent_wrong_ok", True) else 1
    if args.bench_cmd == "critique-eval":
        from verinoda.benchmark import critique_eval

        res = critique_eval.evaluate()
        _bench_out(args.out, res)
        _emit(args, _bench_compact(res, args.out),
              lambda r: (print("critique evaluation (in-sample labelled set)"
                               + (f" (full result: {args.out})" if args.out else "")), _render_flat(r)))
        return 0 if res["benchmark_negatives"]["stated_as_verified"] == 0 else 1
    from verinoda.benchmark import render, run_benchmark

    res = run_benchmark(Path(args.repo or ".").resolve(), questions=args.questions, out=args.out,
                        graphify_cmd=args.graphify_cmd, llm=args.llm, repeat=args.repeat)
    _emit(args, res, render)
    return 0


# -- parser ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verinoda",
        description="Evidence-first codebase analysis (derived from Graphify; not an official Graphify release).",
    )
    p.add_argument("--version", action="version", version=f"verinoda {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    def add(name, fn, help_, repo=True, js=True, parent=None):
        sp = (parent or sub).add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        if repo:
            sp.add_argument("--repo", help="project root (default: nearest dir with .verinoda or .git)")
        if js:
            sp.add_argument("--json", action="store_true", help="print structured JSON")
        return sp

    add("doctor", cmd_doctor, "check installation, index, agent skills and MCP configuration")
    sp = add("setup", cmd_setup, "one step for a project: init + scan/update + skills for the agents found "
                                 "on PATH (safe to re-run)", repo=False)
    sp.add_argument("path", nargs="?", default=".")
    sp.add_argument("--agents", default="auto",
                    help="auto (claude/codex found on PATH), all, none, or a comma list such as claude,codex")
    sp.add_argument("--scope", choices=["project", "user"], default="project")
    sp.add_argument("--no-mcp", action="store_true", help="skills only, no MCP registration")
    sp.add_argument("--allow-home", action="store_true", help="allow setting up the home directory itself")
    sp.add_argument("--reference", action="append", metavar="PATH[=ALIAS,...]",
                    help="a folder of reference code (an original being ported, a vendored copy) that should rank "
                         "below the project's own code unless a question names it or an alias; repeatable")
    sp = add("init", cmd_init, "create .verinoda/ (database + config) in a project", repo=False)
    sp.add_argument("path", nargs="?", default=".")
    sp = add("scan", cmd_scan, "index a repository (AST, no LLM) and record a snapshot", repo=False)
    sp.add_argument("path", nargs="?")
    sp.add_argument("--repo", help="project root (the same as PATH, as for the other commands)")
    sp.add_argument("--force", action="store_true", help="rebuild even if the graph shrinks")
    sp.add_argument("--precise", action="store_true",
                    help="also resolve every call site of the .py files changed since the previous snapshot "
                         "(jedi, cached; needs the precise extra)")
    sp.add_argument("--scip", metavar="FILE",
                    help="use this SCIP index (copied to .verinoda/index/index.scip; freshness reported)")
    sp = add("update", cmd_update, "re-index changed files, record a snapshot, mark affected claims stale", repo=False)
    sp.add_argument("path", nargs="?", help="project root (default: nearest dir with .verinoda or .git)")
    sp.add_argument("--repo", help="project root (the same as PATH, as for the other commands)")
    sp = add("ui", cmd_ui, "notes and graph of the project in the browser (local, read-only)", repo=False, js=False)
    sp.add_argument("path", nargs="?", help="project root (default: nearest dir with .verinoda or .git)")
    sp.add_argument("--repo", help="project root (the same as PATH, as for the other commands)")
    sp.add_argument("--port", type=_port, default=0, help="port on 127.0.0.1 (default: a free one)")
    sp.add_argument("--no-browser", action="store_true", help="print the address, do not open a browser")
    sp = add("map", cmd_map, "top-down architecture views", repo=False)
    sp.add_argument("path", nargs="?", default=".")
    sp.add_argument("--repo", help="project root (the same as PATH, as for the other commands)")
    sp.add_argument("--view", choices=["hierarchy", "dependencies", "dataflow", "config", "tests", "history", "impact"])
    sp.add_argument("--target", action="append", help="impact view: file or symbol (repeatable); default: git changes")
    sp.add_argument("--base", help="impact view: diff base (default HEAD + untracked)")
    sp.add_argument("--max-lines", type=int, default=60)
    sp = add("query", cmd_query, "bounded, justified retrieval for a question (plain text; --json for programs)")
    sp.add_argument("question")
    sp.add_argument("--max-items", type=int, default=10)
    sp.add_argument("--max-chars", type=int, default=6000)
    sp = add("trace", cmd_trace, "directed paths between two symbols/files with edge locations")
    sp.add_argument("source")
    sp.add_argument("target")
    sp.add_argument("--mode", choices=["flow", "any"], default="flow")
    sp = add("analyze", cmd_analyze, "answer a question as claims with evidence, critique and unknowns")
    sp.add_argument("question", nargs="?", help="the question (optional with --plan: the plan's user_message)")
    sp.add_argument("--plan", metavar="FILE",
                    help="a checked question plan file (under .verinoda/plans/; never JSON on the command line)")
    sp.add_argument("--run-tests", action="store_true", help="run the tests that reach the answer (isolated)")
    sp.add_argument("--observe", action="store_true",
                    help="observe the selected tests with the call tracer (runtime evidence, isolated run)")
    sp.add_argument("--no-challenge", action="store_true")
    sp.add_argument("--budget-seconds", type=float, default=60)
    sp.add_argument("--budget-calls", type=int, default=40)
    sp.add_argument("--budget-tokens", type=int, default=6000)

    sp = sub.add_parser("plan", help="question plans: draft, check, schema, audit (exit 0 ready, 2 invalid, "
                                     "3 needs clarification)")
    psub = sp.add_subparsers(dest="plan_cmd", required=True)
    c = add("draft", cmd_plan, "draft a plan from rules and write it under .verinoda/plans/", parent=psub)
    c.add_argument("message", help="the user's message, verbatim")
    c.add_argument("--out", help="plan file to write (default: .verinoda/plans/plan-NNN.json; a bare file "
                                 "name is written under .verinoda/plans/)")
    c = add("check", cmd_plan, "validate and ground a plan file, store it (exit 0 ready, 2 invalid, "
                               "3 needs clarification)", parent=psub)
    c.add_argument("file", help="plan file (absolute, repository-relative or a name under .verinoda/plans/)")
    add("schema", cmd_plan, "print the JSON Schema of verinoda.question_plan/1 (always JSON)", repo=False,
        parent=psub)
    c = add("audit", cmd_plan, "re-judge an analysis' sub-questions on the current claim statuses", parent=psub)
    c.add_argument("analysis_id")
    c.add_argument("--no-refresh", action="store_true", help="do not update the index first")

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
            c.add_argument("--kind", choices=CLAIM_ADD_KINDS, default="general",
                           help="claim kind, graded by its own predicate: location (a definition of --symbol "
                                "spans the source lines), relation (the source line calls --symbol), config "
                                "(the source line reads env var --symbol); general = term coverage")
            c.add_argument("--symbol", help="the symbol / call target / env var the claim is about")

    sp = add("verify", cmd_verify, "re-check a claim's evidence against the current tree")
    sp.add_argument("id")
    sp.add_argument("--run", action="store_true", help="also re-run the claim's recorded test experiment")
    sp = add("challenge", cmd_challenge, "adversarial check; lowers confidence when evidence is weak")
    sp.add_argument("id")
    sp = add("resolve", cmd_resolve, "pin every reference in a message (repos, files, versions, packages, PRs, "
                                     "papers) to the exact version meant (exit 0 complete, 3 partial/questions)")
    sp.add_argument("text", help="the user's message, verbatim")
    sp.add_argument("--reference", action="append", metavar="URL[@ref]",
                    help="an extra reference the user gave outside the text (repeatable)")
    sp.add_argument("--network", choices=NETWORK_CHOICES,
                    help="off | cache (offline-first HTTP cache) | on (default: config research.network)")
    sp.add_argument("--local-intent", action="store_true",
                    help="the question is about this project's behaviour: prefer the version it uses")
    sp = add("research", cmd_research, "inspect a reference repo (pinned commit) or document URL")
    sp.add_argument("reference", nargs="?")
    sp.add_argument("--ref", help="branch/tag/commit to pin (default: the reference's default branch HEAD)")
    sp.add_argument("--topic", help="mechanism to trace in the reference")
    sp.add_argument("--kind", choices=["auto", "official_doc", "standard", "paper", "secondary", "reference_repo"],
                    default="auto")
    sp.add_argument("--resolution", metavar="ID", help="research a reference pinned by `verinoda resolve` "
                                                        "(its resolution id, rrs_...)")
    sp.add_argument("--reference-id", metavar="rN", help="which reference of --resolution (r1, r2, ...)")
    sp.add_argument("--no-verify-source", action="store_true",
                    help="packages: do not download the sdist to compare it with the pinned commit")
    sp = add("compare", cmd_compare, "compare a mechanism's assumptions between a local repo and a reference",
             repo=False)
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
            c.add_argument("--reference", action="append", metavar="URL[@ref]",
                           help="repo URL/path, package or document URL (repeatable; --ref applies to the first)")
            c.add_argument("--ref")
            c.add_argument("--correction", help="the corrected statement the user proposes")
            c.add_argument("--expect-pattern", help="regex the user says occurs (checkable proposition)")
            c.add_argument("--expect-in", help="file glob where --expect-pattern should occur")
            c.add_argument("--topic")
            c.add_argument("--process", action="store_true", help="run the verification protocol now")
            c.add_argument("--network", choices=NETWORK_CHOICES, help="with --process: reference resolution network "
                                                                      "mode (default: config research.network)")
        elif name == "process":
            c.add_argument("id")
            c.add_argument("--topic")
            c.add_argument("--network", choices=NETWORK_CHOICES,
                           help="reference resolution network mode (default: config research.network)")
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

    sp = add("observe", cmd_observe, "run tests under the call tracer (isolated copy) and summarise what they "
                                     "reached (exit 3 when the trace is incomplete)")
    sp.add_argument("test_ids", nargs="*", metavar="TEST_ID",
                    help="pytest node ids or paths (default: tests selected for --for, else the whole suite)")
    sp.add_argument("--for", dest="for_symbols", nargs="+", action="extend", metavar="SYMBOL",
                    help="symbols to report reach for (and to select tests by, when no TEST_ID is given)")
    sp.add_argument("--terms", nargs="+", action="extend", metavar="WORD",
                    help="extra words that select tests by name")
    sp.add_argument("--timeout", type=float, help="seconds (default: twice the experiment timeout)")
    sp.add_argument("--mode", choices=TRACE_MODES, default="auto",
                    help="tracer: auto (sys.monitoring, else setprofile), monitoring, setprofile, off (baseline)")
    sp = add("resolve-call", cmd_resolve_call, "precise resolution of one call site: which definition does "
                                               "TARGET on PATH:LINE bind to? (exit 3: no precise answer)")
    sp.add_argument("site", metavar="PATH:LINE")
    sp.add_argument("target", metavar="TARGET", help="the called name (label, Class.method or name)")
    sp.add_argument("--target", dest="target_at", metavar="PATH:LINE",
                    help="the definition the graph claims; adds a confirms/refutes/undetermined verdict")

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

    for name, fn, help_ in (("install", cmd_install, "install the Verinoda skill (+MCP) for an agent"),
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
                                       "installer/hook commands and commands writing under ~/.graphify are "
                                       "blocked - use `verinoda install` / `verinoda research`)")
    sp.set_defaults(fn=cmd_index)
    sp.add_argument("rest", nargs=argparse.REMAINDER)

    sp = sub.add_parser("benchmark", help="reproducible comparison: raw search vs Graphify baseline vs Verinoda; "
                                          "staleness and critique harnesses")
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
    c = bsub.add_parser("sanitize", help="replace machine paths in existing result files (run where they were "
                                         "produced)")
    c.set_defaults(fn=cmd_benchmark)
    c.add_argument("results", nargs="+")
    c.add_argument("--json", action="store_true")
    c = bsub.add_parser("staleness", help="claim-staleness harness: history replay or mutation suite")
    ssub = c.add_subparsers(dest="stale_cmd", required=True)
    r = ssub.add_parser("replay", help="replay a repository's history against the from-scratch oracle")
    r.set_defaults(fn=cmd_benchmark)
    r.add_argument("--repo", required=True)
    r.add_argument("--commits", type=int, default=300)
    r.add_argument("--pathspec", default="*.py")
    r.add_argument("--cap", type=int, default=40, help="claims per kind per file (0 = all)")
    r.add_argument("--out", help="write the full result JSON here")
    r.add_argument("--json", action="store_true")
    r = ssub.add_parser("mutations", help="the mutation suite on examples/orders_app")
    r.set_defaults(fn=cmd_benchmark)
    r.add_argument("--out", help="write the full result JSON here")
    r.add_argument("--json", action="store_true")
    c = bsub.add_parser("critique-eval", help="critique precision/recall on the labelled claim set")
    c.set_defaults(fn=cmd_benchmark)
    c.add_argument("--out", help="write the full result JSON here")
    c.add_argument("--json", action="store_true")
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
    except KeyError as exc:  # an unknown id: its message, not the repr of the key
        print(f"error: {exc.args[0] if len(exc.args) == 1 else exc}", file=sys.stderr)
        return 1
    except (ValueError, re.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
