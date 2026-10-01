"""What changed at runtime between two traced runs of the same tests: a base revision and the head.

:func:`observe_pair` runs the selected tests twice under the call tracer (with the runtime-flaws recorder):
once on the files of the base commit (an isolated copy, ``observe(ref=...)``) and once on the working tree.
:func:`compare` then lists what one run did and the other did not:

* **calls**: project functions started from a caller (``caller qual -> callee qual``; lines are not part of
  the key, so a call that moved is not a change); calls into test code are left out;
* **library calls**: calls from project code into code outside the repository (``requests.get``,
  ``sqlite3.Connection.execute``);
* **SQL**: normalised statements (literals as ``?``) with how often they ran, statements whose count moved by
  half or more, and the N+1 groups of each run;
* **routes**: HTTP route handlers (the cross-service route table of the current code) the tests reached;
* **exceptions**: exception types raised in project code (``sys.monitoring`` only), and the exceptions that
  ended a test phase;
* **test outcomes** that differ.

Each item is ``observed`` in one run and absent from the other: "the head run (commit C, tests T) made this
call; the base run of the same tests did not". That holds only for these runs: an absence is reported as
observed only when the run it is absent from is complete (else ``unknown``: that run may have made it). A
difference can come from the tests themselves being flaky; it never says what always happens.
"""

from __future__ import annotations

import json
from pathlib import Path

from verinoda.runtime import flaws as flawmod
from verinoda.runtime import trace as rt

MAX_ITEMS = 40          # items per list in the result (totals are always given)
SQL_CHANGE_RATIO = 1.5  # executions moved by half or more ...
SQL_CHANGE_MIN = 3      # ... and by at least this many
LIMITS = [
    "a difference is observed for these two runs of the selected tests only, never what always happens; a flaky "
    "test can make one",
    "calls are matched by file and qualified name, not line: a function that moved file or was renamed shows as "
    "removed and added",
    "graph nodes and routes come from the current code (its graph and route table): a route only the base had is "
    "not named",
    "exceptions raised in project code are recorded with sys.monitoring (Python 3.12+) only",
    "child processes started by tests are not traced",
]


def _header(run: dict) -> dict:
    h = run.get("header") or {}
    if isinstance(h, str):
        try:
            h = json.loads(h)
        except ValueError:
            h = {}
    return h if isinstance(h, dict) else {}


def _list(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def _dict(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _tests_of(contexts) -> set[str]:
    return {t for t in (rt._ctx_test(c) for c in contexts) if t}


class RunView:
    """One run as the comparison reads it."""

    def __init__(self, store, run_id: str, repo: Path, mapper=None):
        run = rt.load_run(store, run_id)
        if run is None:
            raise ValueError(f"no runtime run {run_id}")
        self.id, self.repo = run_id, repo
        self.h = _header(run)
        self.commit = run.get("commit_sha")
        self.ref = self.h.get("ref")
        self.complete = bool(run.get("complete"))
        self.outcomes = {t: v for t, v in _dict(self.h.get("tests_outcomes")).items() if isinstance(v, dict)}
        self.calls: dict[tuple, dict] = {}
        self.library: dict[tuple, dict] = {}
        self.reached: dict[tuple, set[str]] = {}   # (callee path, callee qual, callee line) -> tests
        for c in run["calls"]:
            tests = _tests_of(_list(c.get("tests")))
            cp, cq, tp, tq = c["caller_path"], c["caller_qual"], c["callee_path"], c["callee_qual"]
            if tp == rt.EXT:
                if cp == rt.EXT or rt.is_test_path(cp):
                    continue
                key = (cp, cq, tq)
                d = self.library.setdefault(key, {"hits": 0, "tests": set(), "at": f"{cp}:{c['caller_line']}"})
            else:
                if rt.is_test_path(tp):
                    continue
                key = (cp, cq, tp, tq)
                d = self.calls.setdefault(key, {"hits": 0, "tests": set(),
                                                "at": f"{cp}:{c['caller_line']}" if cp != rt.EXT else None,
                                                "def": f"{tp}:{c['callee_line']}"})
                self.reached.setdefault((tp, tq, int(c["callee_line"] or 0)), set()).update(tests)
            d["hits"] += int(c.get("hits") or 0)
            d["tests"] |= tests
        self.raises_recorded = bool(self.h.get("raises_recorded"))
        stats = _dict(self.h.get("stats"))
        self.raises_cut = (int(self.h.get("raises_total") or 0) > len(_list(self.h.get("raises")))
                           or int(stats.get("raises_dropped") or 0) > 0)
        self.raises: dict[tuple, dict] = {}
        for r in _list(self.h.get("raises")):
            site = r.get("site") or []
            if len(site) != 3 or site[0] == rt.EXT or rt.is_test_path(site[0]):
                continue
            key = (site[0], site[2], r.get("type") or "?")
            d = self.raises.setdefault(key, {"hits": 0, "tests": set(), "at": f"{site[0]}:{site[1]}"})
            d["hits"] += int(r.get("hits") or 0)
            d["tests"] |= set(r.get("tests") or [])
        self.flaws = _dict(self.h.get("runtime_flaws"))
        self.sql: dict[str, dict] | None = None
        self.sql_complete = False
        path = self.h.get("flaws_path")
        if path and Path(path).is_file():
            try:
                parsed = flawmod.parse(Path(path).read_bytes())
            except (OSError, ValueError):
                parsed = None
            if parsed is not None:
                self.sql = _sql(parsed)
                self.sql_complete = bool(parsed["header"].get("complete"))

    def meta(self) -> dict:
        return {"run_id": self.id, "commit": self.commit, **({"ref": self.ref} if self.ref else {}),
                "complete": self.complete, "tests": len(self.outcomes)}


def _sql(parsed: dict) -> dict[str, dict]:
    """Statement -> executions, tests and the innermost project (non-test) frame that ran it."""
    out: dict[str, dict] = {}
    for rec in parsed["sql"]:
        stmt = rec.get("stmt") or ""
        d = out.setdefault(stmt, {"executions": 0, "tests": set(), "at": None})
        d["executions"] += int(rec.get("n") or 0)
        t = flawmod._test_of(rec["ctx"])
        if t:
            d["tests"].add(t[0])
        if d["at"] is None:
            frames = rec.get("stack") or []
            f = next((f for f in frames if not rt.is_test_path(f["path"])), frames[0] if frames else None)
            if f:
                d["at"] = f"{f['path']}:{f['line']}"
    return out


def _item(kind: str, what: str, d: dict, *, present: RunView, absent: RunView, common: set[str], **extra) -> dict:
    tests = sorted(d["tests"])
    only_new = bool(tests) and not (set(tests) & common)
    if not absent.complete:
        status, basis = "unknown", (f"seen in run {present.id}; run {absent.id} is incomplete, so it may have "
                                    "made it too")
    elif kind == "exception" and absent.raises_cut:
        status, basis = "unknown", (f"seen in run {present.id}; run {absent.id} kept only part of its raise "
                                    "sites, so it may have raised it too")
    else:
        status, basis = "observed", f"seen in run {present.id}, not in run {absent.id} of the same tests"
    return {"kind": kind, "what": what, **({"at": d["at"]} if d.get("at") else {}), "hits": d.get("hits"),
            "tests": tests[:5], "n_tests": len(tests), **({"new_tests_only": True} if only_new else {}),
            "status": status, "basis": basis, **extra}


def _diff(kind: str, a: dict, b: dict, base: RunView, head: RunView, common: set[str], label) -> dict:
    added = [_item(kind, label(k), b[k], present=head, absent=base, common=common) for k in sorted(set(b) - set(a))]
    removed = [_item(kind, label(k), a[k], present=base, absent=head, common=common) for k in sorted(set(a) - set(b))]
    def order(src: dict):  # SQL by executions (its items carry no hits)
        return lambda i: (i.get("new_tests_only", False),
                          -int(src[i["what"]]["executions"] if kind == "sql" else i.get("hits") or 0), i["what"])

    added.sort(key=order(b))
    removed.sort(key=order(a))
    return {"added": added[:MAX_ITEMS], "added_total": len(added), "removed": removed[:MAX_ITEMS],
            "removed_total": len(removed)}



def _routes(view: RunView, mapper, table: list[dict]) -> dict[str, dict]:
    if mapper is None or not table:
        return {}
    by_node: dict[str, list[dict]] = {}
    for r in table:
        by_node.setdefault(r["handler"], []).append(r)
    out: dict[str, dict] = {}
    for (path, qual, line), tests in view.reached.items():
        node = mapper.callee(path, line, qual)
        for r in by_node.get(node, ()):
            ms = "|".join(r["methods"]) if r.get("methods") else "ANY"
            d = out.setdefault(f"{ms} {r['path']}", {"hits": 0, "tests": set(), "at": r.get("at")})
            d["tests"] |= tests
    return out


def compare(store, repo: Path, base_run: str, head_run: str, *, graph=None, route_table: list[dict] | None = None
            ) -> dict:
    """The runtime differences between ``base_run`` and ``head_run`` (see the module docstring)."""
    repo = Path(repo).resolve()
    mapper = rt._Mapper(graph, rt._Sources(repo)) if graph is not None else None
    base, head = RunView(store, base_run, repo), RunView(store, head_run, repo)
    common = set(base.outcomes) & set(head.outcomes)
    out: dict = {"base": base.meta(), "head": head.meta(), "tests_compared": len(common),
                 "tests_only_in_base": sorted(set(base.outcomes) - common)[:20],
                 "tests_only_in_head": sorted(set(head.outcomes) - common)[:20]}
    out["calls"] = _diff("call", base.calls, head.calls, base, head, common,
                         lambda k: f"{k[0]}::{k[1]} -> {k[2]}::{k[3]}" if k[0] != rt.EXT else f"<ext> {k[1]} -> "
                                                                                                 f"{k[2]}::{k[3]}")
    out["library_calls"] = _diff("library_call", base.library, head.library, base, head, common,
                                 lambda k: f"{k[0]}::{k[1]} -> {k[2]}")
    if base.raises_recorded and head.raises_recorded:
        out["exceptions"] = _diff("exception", base.raises, head.raises, base, head, common,
                                  lambda k: f"{k[2]} raised in {k[0]}::{k[1]}")
    else:
        out["exceptions"] = {"recorded": False, "why": "exceptions raised in project code are recorded with "
                                                       "sys.monitoring only (Python 3.12+); a run used another "
                                                       "tracer"}
    if base.sql is not None and head.sql is not None:
        sql = _diff("sql", base.sql, head.sql, base, head, common, lambda k: k)
        changed = []
        for stmt in sorted(set(base.sql) & set(head.sql)):
            a, b = base.sql[stmt]["executions"], head.sql[stmt]["executions"]
            lo, hi = min(a, b), max(a, b)
            if hi - lo >= SQL_CHANGE_MIN and hi >= SQL_CHANGE_RATIO * max(lo, 1):
                changed.append({"kind": "sql", "what": stmt, "base_executions": a, "head_executions": b,
                                "at": head.sql[stmt]["at"] or base.sql[stmt]["at"],
                                "tests": sorted(head.sql[stmt]["tests"] | base.sql[stmt]["tests"])[:5],
                                "status": "observed" if base.sql_complete and head.sql_complete else "unknown",
                                "basis": f"executions in run {base.id}: {a}, in run {head.id}: {b}"})
        changed.sort(key=lambda c: -abs(c["head_executions"] - c["base_executions"]))
        sql["changed"], sql["changed_total"] = changed[:MAX_ITEMS], len(changed)
        for item in sql["added"] + sql["removed"]:
            item["executions"] = item.pop("hits", None)
        for item in sql["added"]:
            item["executions"] = head.sql[item["what"]]["executions"]
        for item in sql["removed"]:
            item["executions"] = base.sql[item["what"]]["executions"]
        nb = {g.get("statement") for g in base.flaws.get("n_plus_one") or []}
        nh = {g.get("statement") for g in head.flaws.get("n_plus_one") or []}
        sql["n_plus_one_added"] = sorted(s for s in nh - nb if s)
        sql["n_plus_one_removed"] = sorted(s for s in nb - nh if s)
        if not (base.sql_complete and head.sql_complete):
            sql["note"] = "a run's SQL recording is incomplete: its counts are lower bounds"
        out["sql"] = sql
    else:
        out["sql"] = {"recorded": False, "why": "a run has no SQL recording (observe with flaws)"}
    if route_table:
        out["routes"] = _diff("route", _routes(base, mapper, route_table), _routes(head, mapper, route_table),
                              base, head, common, lambda k: k)
    tests = []
    for t in sorted(common):
        ob, oh = rt.phase_outcome(base.outcomes[t].get("phases") or {}), rt.phase_outcome(
            head.outcomes[t].get("phases") or {})
        eb, eh = _exc(base.outcomes[t]), _exc(head.outcomes[t])
        if ob != oh or _exc(base.outcomes[t], key=True) != _exc(head.outcomes[t], key=True):
            tests.append({"test": t, "base": ob, "head": oh, **({"base_exception": eb} if eb else {}),
                          **({"head_exception": eh} if eh else {}), "status": "observed",
                          "basis": f"run {base.id} and run {head.id}"})
    out["test_outcomes"] = tests
    out["counts"] = {k: (out[k].get("added_total", 0) + out[k].get("removed_total", 0)
                         + out[k].get("changed_total", 0)) for k in ("calls", "library_calls", "exceptions", "sql",
                                                                      "routes") if k in out}
    if isinstance(out.get("sql"), dict) and "n_plus_one_added" in out["sql"]:
        out["counts"]["sql"] += len(out["sql"]["n_plus_one_added"]) + len(out["sql"]["n_plus_one_removed"])
    out["counts"]["test_outcomes"] = len(tests)
    out["changed"] = any(out["counts"].values())
    out["limits"] = list(LIMITS)
    if not (base.complete and head.complete):
        out["limits"].append("a run is incomplete: what it lacks is unknown, not absent")
    if base.raises_cut or head.raises_cut:
        out["limits"].append("a run kept only part of its raise sites: an exception missing from it is unknown")
    return out


def _exc(outcome: dict, *, key: bool = False):
    """The exception that ended a failed phase: ``type at path:line`` to show, or with ``key`` the
    (type, path, function) compared between runs - a line that moved is not a change."""
    exc = outcome.get("exc") or {}
    for phase in ("call", "setup", "teardown"):
        if isinstance(exc.get(phase), dict):
            e = exc[phase]
            at = e.get("at") if isinstance(e.get("at"), list) else []
            if key:
                return (e.get("type", "?"), at[0] if at else None, at[2] if len(at) > 2 else None)
            return e.get("type", "?") + (f" at {at[0]}:{at[1]}" if len(at) >= 2 else "")
    return None


def observe_pair(store, repo: Path, test_ids, base_ref: str, *, graph=None, timeout: float | None = None,
                 flaws: bool = True, head: dict | None = None, route_table: list[dict] | None = None,
                 mode: str = "auto", flaw_thresholds: dict | None = None, limits: dict | None = None) -> dict:
    """Run ``test_ids`` at ``base_ref`` (and on the working tree unless ``head``, an observe result of it, is
    given) and compare the two runs. ``mode``, ``flaw_thresholds`` and ``limits`` are the tracer settings of
    both runs (give the ones the head was made with)."""
    from verinoda import treestate

    try:
        commit = treestate.resolve_commit(repo, base_ref)
    except Exception as exc:  # noqa: BLE001 - not a commit: said, never a crash of the caller
        return {"error": f"{base_ref} does not name a commit: {exc}"[:300]}
    head = head or rt.observe(store, repo, test_ids, timeout=timeout, graph=graph if graph is not None else False,
                              flaws=flaws, mode=mode, flaw_thresholds=flaw_thresholds, limits=limits)
    if not head.get("run_id"):
        return {"error": f"the head run did not record: {head.get('error')}", "head_run": None}
    ids = list(head.get("tests_requested") or test_ids)
    src = rt._Sources(repo, commit)
    kept = [t for t in ids if _exists_at(repo, commit, t, src)]
    if ids and not kept:
        return {"error": f"none of the selected tests exists at {base_ref}: they are new", "head_run": head["run_id"],
                "next_step": "nothing to compare at run time; the head run alone is in observe"}
    base = rt.observe(store, repo, kept, timeout=timeout, graph=False, flaws=flaws, ref=base_ref, mode=mode,
                      flaw_thresholds=flaw_thresholds, limits=limits)
    if not base.get("run_id") or base.get("error"):
        return {"error": f"the base run at {base_ref} did not record: {base.get('error')}",
                "next_step": base.get("next_step") or "check that the tests run at that commit",
                "base_run": base.get("run_id"), "head_run": head["run_id"]}
    try:
        out = compare(store, repo, base["run_id"], head["run_id"], graph=graph, route_table=route_table)
    except ValueError as exc:  # a run that was not recorded
        return {"error": str(exc), "base_run": base["run_id"], "head_run": head["run_id"]}
    if len(kept) < len(ids):
        out["tests_new_in_head"] = [t for t in ids if t not in kept][:20]
    if out["tests_compared"] == 0:
        return {"error": f"no test ran in both runs (base {out['base']['tests']}, head {out['head']['tests']} "
                         "test(s)): nothing to compare", "base_run": base["run_id"], "head_run": head["run_id"],
                "next_step": "read the base run's log: the tests may not run at that commit"}
    return out


def _exists_at(repo: Path, commit: str, test_id: str, src=None) -> bool:
    """The test a pytest id names exists at ``commit``: its file, and its class and function names (a
    parametrize suffix is not checked). A file that cannot be parsed there counts as present."""
    import ast

    from verinoda.snapshot import git

    path, _, rest = test_id.partition("::")
    path = path.replace("\\", "/").rstrip("/")
    if not path:
        return True
    if git(repo, "cat-file", "-e", f"{commit}:{path}") is None:
        return False
    names = [n.split("[", 1)[0] for n in rest.split("::") if n] if rest else []
    if not names or src is None:
        return True
    tree = src.tree(path)
    if tree is None:
        return True
    body = tree.body
    for name in names:
        hit = next((n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and n.name == name), None)
        if hit is None:
            return False
        body = hit.body if isinstance(hit, ast.ClassDef) else []
    return True


def route_table(repo: Path, graph) -> list[dict]:
    """The current code's HTTP route table (``verinoda routes``), facts reused from the index sidecar."""
    from verinoda import cross_service, index

    try:
        side = index._read_sidecar(repo) or {}
        block = side.get("cross_service") or {}
        old = block.get("files") if block.get("facts_version") == cross_service.FACTS_VERSION else None
        _files, _edges, report, _parsed = cross_service.collect(graph, old=old)
    except Exception:  # noqa: BLE001 - no table: routes are left out of the comparison
        return []
    return report.get("route_table") or []


def render(d: dict) -> list[str]:
    """Text lines for a comparison."""
    if d.get("error"):
        return [f"runtime diff: {d['error']}"] + ([f"  next: {d['next_step']}"] if d.get("next_step") else [])
    b, h = d["base"], d["head"]
    out = [f"runtime diff: base run {b['run_id']} ({(b.get('commit') or '?')[:10]}"
           + ("" if b["complete"] else ", incomplete") + f") vs head run {h['run_id']} ("
           + ((h.get('commit') or '?')[:10] if h.get("ref") else "working tree")
           + ("" if h["complete"] else ", incomplete") + f"), {d['tests_compared']} test(s) in both"]
    if not d.get("changed"):
        out.append("  no runtime change observed between the two runs")
    names = {"calls": "calls", "library_calls": "library calls", "sql": "SQL", "routes": "routes",
             "exceptions": "exceptions raised"}
    for key, name in names.items():
        sec = d.get(key) or {}
        if sec.get("recorded") is False:
            out.append(f"  {name}: not compared ({sec.get('why')})")
            continue
        for sign, part in (("+", "added"), ("-", "removed")):
            items = sec.get(part) or []
            if not items:
                continue
            out.append(f"  {name} {part} ({sec.get(part + '_total', len(items))}):")
            for it in items[:10]:
                extra = f" x{it['executions']}" if key == "sql" and it.get("executions") else ""
                where = f" @{it['at']}" if it.get("at") else ""
                st = "" if it["status"] == "observed" else f" [{it['status']}]"
                new = " (new tests only)" if it.get("new_tests_only") else ""
                out.append(f"    {sign} {it['what']}{extra}{where}{new}{st}")
        for it in (sec.get("changed") or [])[:10]:
            out.append(f"  SQL count {it['base_executions']} -> {it['head_executions']}: {it['what']}"
                       + (f" @{it['at']}" if it.get("at") else ""))
        for s in sec.get("n_plus_one_added") or []:
            out.append(f"  N+1 new in head: {s}")
        for s in sec.get("n_plus_one_removed") or []:
            out.append(f"  N+1 gone in head: {s}")
    for t in d.get("test_outcomes") or []:
        exc = " / ".join(f"{side}: {t[side + '_exception']}" for side in ("base", "head") if t.get(side + "_exception"))
        out.append(f"  test {t['test']}: {t['base']} -> {t['head']}" + (f" ({exc})" if exc else ""))
    return out
