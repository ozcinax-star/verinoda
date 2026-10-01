"""Runtime flaws in a recorded test run: N+1 queries, repeated SQL and slow paths.

:func:`verinoda.runtime.trace.observe` with ``flaws=True`` loads the
``verinoda_flaws`` plugin (:mod:`verinoda.runtime.flaws_plugin`) beside the
call tracer; this module reads the file it leaves and finds:

* **N+1** - one normalised read (``SELECT`` / ``WITH``) executed at least
  ``n_plus_one`` times in one test phase from the same stack, with at least two
  different parameter sets, where a project frame of that stack makes the call
  inside a loop (``for`` / ``while`` / comprehension body, read from the
  file's syntax tree at the recorded position). The path runs from the test to
  the statement; the loop is the innermost one.
* **Repeated SQL** - the same statement text with the same parameters
  executed at least ``repeated`` times in one test phase.
* **Slow paths** - project functions (not test code) whose sampled time in
  one test phase is at least ``slow_ms`` and at least ``slow_share`` of that
  test phase: by self time (the function innermost among project frames,
  library calls included) or total time; a function whose time is explained
  by a slower callee on its path is left out.

What a finding means: ``status: observed`` - in run R this test executed X N
times at path P (run-scoped and existential, never "always"). The reading of
it (``interpretation``: "this is an N+1 query", "the same rows were read
again") is ``strong_inference`` at most, and ``weak_inference`` when the loop
is in the test itself.
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from pathlib import Path

from verinoda import evidence as evmod
from verinoda import testcode

SCHEMA = "verinoda.flaws/1"
FLAWS_FILE = "flaws.jsonl"
PLUGIN_MODULE = "verinoda_flaws"
DEFAULTS = {"n_plus_one": 5, "repeated": 3, "slow_ms": 100.0, "slow_share": 0.2}
MAX_FINDINGS = 50
MAX_PATH = 12          # frames of a call path shown (the outermost and the innermost kept)
READS = ("SELECT", "WITH")
WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "UPSERT", "MERGE")
EXPLAINED = 0.8        # a callee with this share of a function's total time explains it
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
COMPS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
LIMITS_TEXT = [
    "findings are observations of this run: what these tests executed, never what always happens",
    "N+1 and repeated SQL are counted per test phase, not per call of the enclosing function",
    "SQL is recorded for sqlite3 connections (and other drivers through SQLAlchemy engine events); other "
    "database clients are not seen",
    "times of slow paths are sampled estimates (main thread only); SQL durations are measured per statement",
]


def plugin_source() -> bytes:
    return Path(__file__).with_name("flaws_plugin.py").read_bytes()


def thresholds(given: dict | None = None) -> dict:
    out = dict(DEFAULTS)
    for k, v in (given or {}).items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown flaw threshold {k!r} (known: {', '.join(DEFAULTS)})")
        if v is not None:
            out[k] = type(DEFAULTS[k])(v)
    if out["n_plus_one"] < 2 or out["repeated"] < 2:
        raise ValueError("n_plus_one and repeated must be at least 2")
    if out["slow_ms"] < 0 or not 0 <= out["slow_share"] <= 1:
        raise ValueError("slow_ms must be >= 0 and slow_share within 0..1")
    return out


# -- the plugin's file ------------------------------------------------------------------------

def parse(data: bytes) -> dict:
    """``header``, ``sql`` and ``samples`` (stacks as frame dicts, innermost first), ``ctx_ms``."""
    header: dict = {}
    frames: list = []
    sql, samples, ctx_ms = [], [], {}
    raw_recs = []
    for raw in data.decode("utf-8", "replace").splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("k") == "header":
            header = rec
        elif rec.get("k") == "frames":
            frames = rec.get("frames") or []
        else:
            raw_recs.append(rec)
    if header.get("schema") not in (None, SCHEMA):
        raise ValueError(f"unsupported flaws schema {header.get('schema')!r}")
    ctxs = header.get("contexts") or []

    def frame(i):
        f = frames[i] if isinstance(i, int) and 0 <= i < len(frames) else None
        if not f:
            return None
        f = list(f) + [None] * (7 - len(f))
        return {"path": f[0], "line": f[1], "qual": f[2], "def_line": f[3], "col": f[4], "end_line": f[5],
                "end_col": f[6]}

    for rec in raw_recs:
        c = rec.get("ctx")
        name = ctxs[c] if isinstance(c, int) and 0 <= c < len(ctxs) else None
        if name is None:
            continue
        if rec.get("k") == "ctx":
            ctx_ms[name] = float(rec.get("ms") or 0)
            continue
        stack = [f for f in (frame(i) for i in rec.get("stack") or []) if f]
        if rec.get("k") == "sql":
            sql.append({**rec, "ctx": name, "stack": stack})
        elif rec.get("k") == "sample":
            samples.append({**rec, "ctx": name, "stack": stack})
    return {"header": header, "sql": sql, "samples": samples, "ctx_ms": ctx_ms}


def verb(stmt: str) -> str:
    return (stmt.lstrip("( \t").split(None, 1) or [""])[0].upper()


def _test_of(ctx: str) -> tuple[str, str] | None:
    nid, sep, phase = ctx.rpartition("|")
    return (nid, phase) if sep and not ctx.startswith("<") else None


# -- loops from the syntax tree ------------------------------------------------------------

def _contains(node, pos: tuple) -> bool:
    if getattr(node, "lineno", None) is None or getattr(node, "end_lineno", None) is None:
        return False
    line, col, end_line, end_col = pos
    if col is None or end_line is None or end_col is None:
        return node.lineno <= line <= node.end_lineno
    return (node.lineno, node.col_offset) <= (line, col) and (end_line, end_col) <= (node.end_lineno,
                                                                                    node.end_col_offset)


def _chain(tree, pos: tuple) -> list:
    """The nodes from the module down to the innermost one containing ``pos``."""
    chain, node = [tree], tree
    while True:
        nxt = None
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.comprehension):
                if any(_contains(x, pos) for x in (ch.target, ch.iter, *ch.ifs)):
                    nxt = ch
                    break
            elif _contains(ch, pos):
                nxt = ch
                break
        if nxt is None:
            return chain
        chain.append(nxt)
        node = nxt


def enclosing_loop(tree, pos: tuple) -> dict | None:
    """The innermost loop of the same function that runs ``pos`` once per iteration, or None.

    ``pos`` = (line, col, end line, end col) of the call; col None compares lines only. A ``for``
    runs its body per item (not its iterable), a ``while`` its test and body, a comprehension its
    element, its conditions and every ``for`` after the first (not the first iterable)."""
    if tree is None:
        return None
    chain = _chain(tree, pos)
    start = max((i for i, n in enumerate(chain) if isinstance(n, SCOPES)), default=0)
    best = None
    for i in range(start, len(chain) - 1):
        n, ch = chain[i], chain[i + 1]
        if isinstance(n, (ast.For, ast.AsyncFor)) and any(ch is b for b in n.body):
            best = (n, "for")
        elif isinstance(n, ast.While) and (ch is n.test or any(ch is b for b in n.body)):
            best = (n, "while")
        elif isinstance(n, COMPS):
            if isinstance(ch, ast.comprehension):
                first = ch is n.generators[0]
                below = chain[i + 2] if i + 2 < len(chain) else None
                if not first or below is not ch.iter:
                    best = (n, "comprehension")
            else:   # the element (or a dict comprehension's key / value)
                best = (n, "comprehension")
    if best is None:
        return None
    n, kind = best
    return {"line": n.lineno, "end_line": n.end_lineno, "kind": kind}


def _pos(f: dict) -> tuple:
    return (int(f["line"] or 0), f.get("col"), f.get("end_line"), f.get("end_col"))


def _site(f: dict) -> dict:
    return {"path": f["path"], "line": f["line"], "qual": f["qual"]}


def _call_path(stack: list[dict]) -> tuple[list[dict], bool]:
    """Outermost first, at most ``MAX_PATH`` frames (the middle cut); True when cut."""
    frames = [_site(f) for f in reversed(stack)]
    if len(frames) <= MAX_PATH:
        return frames, False
    half = MAX_PATH // 2
    return frames[:half] + frames[-half:], True


def _find_loop(src, stack: list[dict]) -> tuple[dict, dict] | None:
    for f in stack:   # innermost first
        if not f.get("path") or not f.get("line"):
            continue
        loop = enclosing_loop(src.tree(f["path"]), _pos(f))
        if loop is not None:
            return f, loop
    return None


# -- detectors ------------------------------------------------------------------------------------

def _short(stmt: str, n: int = 160) -> str:
    return stmt if len(stmt) <= n else stmt[: n - 3] + "..."


def n_plus_one(parsed: dict, src, run_id: str, th: dict) -> tuple[list[dict], int]:
    """N+1 findings, grouped by (statement, loop site) over the tests; and how many repeated reads had no loop."""
    groups: dict[tuple, dict] = {}
    no_loop = 0
    for rec in parsed["sql"]:
        t = _test_of(rec["ctx"])
        stmt = rec.get("stmt") or ""
        if t is None or verb(stmt) not in READS:
            continue
        n = int(rec.get("n") or 0) - int(rec.get("many") or 0)
        distinct = int(rec.get("distinct") or 0)
        if n < th["n_plus_one"] or (distinct < 2 and not rec.get("distinct_more")):
            continue
        hit = _find_loop(src, rec["stack"])
        if hit is None:
            no_loop += 1
            continue
        frame, loop = hit
        key = (stmt, frame["path"], frame["line"])
        per_test = {"test": t[0], "phase": t[1], "executions": n,
                    "distinct_parameters": f"{distinct}+" if rec.get("distinct_more") else distinct,
                    "ms": rec.get("ms"), "max_ms": rec.get("max_ms")}
        g = groups.get(key)
        if g is None or n > g["executions"]:
            path, cut = _call_path(rec["stack"])
            in_test = testcode.is_test_or_support_file(frame["path"])
            g = groups[key] = {
                **(g or {}), "kind": "n_plus_one", "status": "observed", "run_id": run_id,
                "statement": stmt, "executions": n, "test": t[0], "phase": t[1],
                "loop": {"path": frame["path"], "line": loop["line"], "end_line": loop["end_line"],
                         "kind": loop["kind"], "qual": frame["qual"]},
                "call": {"path": frame["path"], "line": frame["line"]},
                "site": _site(rec["stack"][0]) if rec["stack"] else None,
                "call_path": path, "call_path_cut": cut, "loop_in_test": in_test,
                "cite": f"{frame['path']}:{frame['line']}", "tests": (g or {}).get("tests", []),
                **({"thread": True} if rec.get("thread") else {}),
            }
        g["tests"].append(per_test)
    out = []
    for g in groups.values():
        g["tests"].sort(key=lambda x: (-x["executions"], x["test"]))
        g["n_tests"] = len(g["tests"])
        g["tests"] = g["tests"][:10]
        top = g["tests"][0]
        loop = g["loop"]
        g["observation"] = (f"run {run_id}: {g['test']} executed `{_short(g['statement'])}` {g['executions']} times "
                            f"({top['distinct_parameters']} distinct parameter sets) from the {loop['kind']} loop at "
                            f"{loop['path']}:{loop['line']} (call at {g['cite']})")
        if g["loop_in_test"]:
            g["interpretation"] = {
                "status": "weak_inference",
                "text": "the loop is in test code: the repeated query may be the test's own doing, not the code's",
                "fix": None}
        else:
            g["interpretation"] = {
                "status": "strong_inference",
                "text": f"an N+1 query: one `{verb(g['statement'])}` per iteration of the loop in {loop['qual']} "
                        "(seen in this run; how many rows it costs depends on the data)",
                "fix": "load the rows for all items with one query before the loop (a JOIN, or WHERE key IN (...)), "
                       "or batch-load them (an ORM's selectinload / prefetch_related)"}
        out.append(g)
    out.sort(key=lambda g: (g["loop_in_test"], -g["executions"], g["cite"]))
    return out, no_loop


def repeated_sql(parsed: dict, run_id: str, th: dict) -> list[dict]:
    """The same statement with the same parameters executed ``repeated``+ times in one test phase."""
    per: dict[tuple, dict] = {}
    for rec in parsed["sql"]:
        t = _test_of(rec["ctx"])
        stmt = rec.get("stmt") or ""
        if t is None or verb(stmt) not in READS + WRITES:
            continue
        g = per.setdefault((rec["ctx"], stmt), {"same": defaultdict(int), "best": None, "n": 0})
        for h, k in rec.get("same") or []:
            g["same"][h] += int(k)
        n = int(rec.get("n") or 0)
        g["n"] += n
        if g["best"] is None or n > g["best"]["n"]:
            g["best"] = rec
    groups: dict[tuple, dict] = {}
    for (ctx, stmt), g in per.items():
        reps = sorted((k for k in g["same"].values() if k >= th["repeated"]), reverse=True)
        if not reps or not g["best"]["stack"]:
            continue
        t = _test_of(ctx)
        rec = g["best"]
        site = rec["stack"][0]
        key = (stmt, site["path"], site["line"])
        per_test = {"test": t[0], "phase": t[1], "max_repeats": reps[0], "repeated_parameter_sets": len(reps),
                    "executions": g["n"]}
        cur = groups.get(key)
        if cur is None or reps[0] > cur["max_repeats"]:
            path, cut = _call_path(rec["stack"])
            cur = groups[key] = {
                **(cur or {}), "kind": "repeated_sql", "status": "observed", "run_id": run_id, "statement": stmt,
                "max_repeats": reps[0], "repeated_parameter_sets": len(reps), "test": t[0], "phase": t[1],
                "site": _site(site), "call_path": path, "call_path_cut": cut,
                "cite": f"{site['path']}:{site['line']}", "tests": (cur or {}).get("tests", [])}
        cur["tests"].append(per_test)
    out = []
    for g in groups.values():
        g["tests"].sort(key=lambda x: (-x["max_repeats"], x["test"]))
        g["n_tests"] = len(g["tests"])
        g["tests"] = g["tests"][:10]
        sets = g["repeated_parameter_sets"]
        g["observation"] = (f"run {run_id}: {g['test']} executed `{_short(g['statement'])}` with the same parameters "
                            f"{g['max_repeats']} times" + (f" ({sets} parameter sets repeated)" if sets > 1 else "")
                            + f" (call at {g['cite']})")
        if verb(g["statement"]) in READS:
            g["interpretation"] = {
                "status": "strong_inference",
                "text": "the same rows were read again with the same query: unless they change in between, one "
                        "read would do",
                "fix": "read once and pass the rows on, or cache them for the length of the request"}
        else:
            g["interpretation"] = {"status": "weak_inference",
                                   "text": "the same write was made repeatedly: check that each one is meant",
                                   "fix": None}
        out.append(g)
    out.sort(key=lambda g: (-g["max_repeats"], g["cite"]))
    return out


def effective_sample_ms(parsed: dict) -> float:
    """The mean time between samples (timer resolution and the GIL make it longer than asked for)."""
    n = int((parsed["header"].get("stats") or {}).get("samples") or 0)
    asked = float(parsed["header"].get("sample_ms") or 0)
    return round(max(asked, sum(parsed["ctx_ms"].values()) / n), 1) if n else asked


def slow_paths(parsed: dict, run_id: str, th: dict) -> list[dict]:
    """Project functions whose sampled self or total time in one test phase passes the thresholds."""
    sample_ms = effective_sample_ms(parsed)
    by_ctx: dict[str, list[dict]] = defaultdict(list)
    for rec in parsed["samples"]:
        if _test_of(rec["ctx"]) is not None and rec["stack"]:
            by_ctx[rec["ctx"]].append(rec)
    run_ms = sum(ms for c, ms in parsed["ctx_ms"].items() if _test_of(c) is not None) or 0.0
    out = []
    for ctx, recs in by_ctx.items():
        test_ms = parsed["ctx_ms"].get(ctx) or sum(r["ms"] for r in recs)
        total: dict[tuple, float] = defaultdict(float)
        self_: dict[tuple, float] = defaultdict(float)
        nsamp: dict[tuple, int] = defaultdict(int)
        hot: dict[tuple, dict] = defaultdict(lambda: defaultdict(float))
        dominant: dict[tuple, tuple] = {}
        for r in recs:
            ms = float(r.get("ms") or 0)
            seen = set()
            for depth, f in enumerate(r["stack"]):
                key = (f["path"], f["qual"], f["def_line"])
                if key in seen:
                    continue   # recursion: once per sample
                seen.add(key)
                total[key] += ms
                nsamp[key] += int(r.get("n") or 0)
                if depth == 0:
                    self_[key] += ms
                    hot[key][f["line"]] += ms
                if key not in dominant or ms > dominant[key][0]:
                    dominant[key] = (ms, r["stack"], depth)
        limit = max(th["slow_ms"], th["slow_share"] * test_ms)
        cands = {k for k, v in total.items() if v >= limit and not testcode.is_test_or_support_file(k[0])}
        keep = set()
        for a in cands:
            if self_[a] >= limit:
                keep.add(a)
                continue
            explained = False
            for b in cands:
                if b == a or total[b] < EXPLAINED * total[a]:
                    continue
                _, stack, depth = dominant[b]
                outer = {(f["path"], f["qual"], f["def_line"]) for f in stack[depth + 1:]}
                if a in outer:
                    explained = True
                    break
            if not explained:
                keep.add(a)
        t = _test_of(ctx)
        for k in keep:
            _, stack, depth = dominant[k]
            path, cut = _call_path(stack[depth:])
            is_self = self_[k] >= limit
            line = max(hot[k].items(), key=lambda kv: kv[1])[0] if is_self and hot[k] else stack[depth]["line"]
            f = {"path": k[0], "line": k[2], "qual": k[1]}
            share = total[k] / test_ms if test_ms else None
            out.append({
                "kind": "slow_path", "status": "observed", "run_id": run_id, "test": t[0], "phase": t[1],
                "function": f, "by": "self" if is_self else "total",
                "total_ms": round(total[k], 1), "self_ms": round(self_[k], 1),
                "share_of_test": round(share, 3) if share is not None else None,
                "share_of_run": round(total[k] / run_ms, 3) if run_ms else None,
                "threshold_ms": round(limit, 1), "samples": nsamp[k], "sample_ms": sample_ms,
                "call_path": path, "call_path_cut": cut, "cite": f"{k[0]}:{line}",
                "observation": (f"run {run_id}: {t[0]} spent about {total[k]:.0f} ms in {k[1]} ({k[0]}:{k[2]}), "
                                f"about {self_[k]:.0f} ms of it in its own lines and the library calls they make"
                                + (f", {share:.0%} of the test phase" if share is not None else "")
                                + f" (sampled about every {sample_ms:g} ms; hottest line {k[0]}:{line})"),
            })
    out.sort(key=lambda x: (-x["total_ms"], x["cite"]))
    return out


def statements(parsed: dict, limit: int = 10) -> list[dict]:
    agg: dict[str, list] = {}
    for rec in parsed["sql"]:
        a = agg.setdefault(rec.get("stmt") or "", [0, 0.0, set()])
        a[0] += int(rec.get("n") or 0)
        a[1] += float(rec.get("ms") or 0)
        t = _test_of(rec["ctx"])
        if t:
            a[2].add(t[0])
    rows = [{"statement": _short(s, 200), "executions": n, "ms": round(ms, 2), "n_tests": len(ts)}
            for s, (n, ms, ts) in agg.items()]
    rows.sort(key=lambda r: (-r["executions"], r["statement"]))
    return rows[:limit]


# -- evidence and the report -----------------------------------------------------------------------------

def finding_evidence(repo: Path, finding: dict, commit: str | None) -> dict | None:
    """Experiment evidence anchored on the cited line (not inserted); None when it cannot be cited."""
    path, _, line = finding["cite"].rpartition(":")
    meta = {"kind": "runtime_flaw", "flaw": finding["kind"], "run_id": finding["run_id"], "test": finding["test"],
            "outcome": "pass",
            "scope": "existential: observed in this run at this commit; never evidence for 'always'"}
    try:
        ev = evmod.source_evidence(Path(repo), path, int(line), commit=commit, source_type="experiment", meta=meta)
    except (OSError, ValueError):
        return None
    if ev is not None:
        ev["locator"] = f"run {finding['run_id']}: {finding['cite']} ({finding['kind']})"
    return ev


FINDING_KEYS = ("n_plus_one", "repeated_sql", "slow_paths")


def without_evidence(block: dict) -> dict:
    """The block as kept in the run's header: the findings without their evidence records."""
    return {k: ([{x: y for x, y in f.items() if x != "evidence"} for f in v] if k in FINDING_KEYS else v)
            for k, v in block.items()}


def report(parsed: dict | None, src, run_id: str, th: dict, *, repo: Path | None = None,
           commit: str | None = None, why_missing: str | None = None, with_evidence: bool = True) -> dict:
    """The ``runtime_flaws`` block of an observe result."""
    if parsed is None:
        return {"recorded": False, "run_id": run_id, "why": why_missing or "the flaws plugin wrote no file",
                "thresholds": th}
    h = parsed["header"]
    nplus, no_loop = n_plus_one(parsed, src, run_id, th)
    reps = repeated_sql(parsed, run_id, th)
    loops = {(g["statement"], g["cite"]) for g in nplus}
    for g in reps:
        if (g["statement"], g["cite"]) in loops:
            g["also_n_plus_one"] = True
    slow = slow_paths(parsed, run_id, th)
    if with_evidence and repo is not None:
        for f in nplus[:MAX_FINDINGS] + reps[:MAX_FINDINGS] + slow[:MAX_FINDINGS]:
            f["evidence"] = finding_evidence(repo, f, commit)
    limits = list(LIMITS_TEXT)
    if not h.get("complete"):
        limits.append(f"the flaws recording is incomplete ({h.get('breach') or 'not finished'}): counts are lower "
                      "bounds")
    if h.get("samples_complete") is False:
        limits.append("some sampled stacks were left out (a budget): slow-path times are lower bounds")
    if no_loop:
        limits.append(f"{no_loop} read(s) repeated {th['n_plus_one']}+ times from one stack had no loop in a project "
                      "frame (a callback from library code, recursion, map()): not reported as N+1")
    stats = h.get("stats") or {}
    return {
        "recorded": True, "run_id": run_id, "complete": bool(h.get("complete")), "breach": h.get("breach"),
        "hooks": h.get("hooks") or [], "sample_ms": h.get("sample_ms"),
        "effective_sample_ms": effective_sample_ms(parsed), "thresholds": th,
        "n_plus_one": nplus[:MAX_FINDINGS], "n_plus_one_total": len(nplus),
        "repeated_sql": reps[:MAX_FINDINGS], "repeated_sql_total": len(reps),
        "slow_paths": slow[:MAX_FINDINGS], "slow_paths_total": len(slow),
        "sql": {"executions": stats.get("sql_executions", sum(int(r.get("n") or 0) for r in parsed["sql"])),
                "statements": statements(parsed)},
        "samples": stats.get("samples"),
        "limits": limits,
    }
