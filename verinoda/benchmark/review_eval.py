"""The review fixtures harness (docs/DESIGN.md D35): ``verinoda benchmark review-eval``.

Each fixture of ``benchmarks/review_fixtures/<split>.json`` is applied to a fresh git copy of its project
(an indexed base copy is made once per project and copied per fixture), reviewed with
:func:`verinoda.review.review`, and scored against its gold labels (the matching rules are in
``benchmarks/review_fixtures/README.md``). The copies live in a work directory the caller names
(default: a temporary one); nothing is written into the repository. It needs a source checkout (the fixtures
and examples/ are not in the wheel). ``vcopy`` fixtures need a git
repository to clone (``--vcopy``); without it they are reported as skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from verinoda import review_rules as rr

HERE = Path(__file__).resolve().parents[2]
FIXTURES = HERE / "benchmarks" / "review_fixtures"
EXAMPLES = HERE / "examples"
_GIT_ENV = {"GIT_AUTHOR_NAME": "review-eval", "GIT_AUTHOR_EMAIL": "review-eval@example.invalid",
            "GIT_COMMITTER_NAME": "review-eval", "GIT_COMMITTER_EMAIL": "review-eval@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1"}


def _rmtree(p: Path) -> None:
    """Remove a copy, read-only git objects included (Windows)."""
    import stat

    def unlock(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    if Path(p).exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=unlock)
        else:
            shutil.rmtree(p, onerror=unlock)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                   capture_output=True, env={**os.environ, **_GIT_ENV})


def _index(repo: Path) -> None:
    from verinoda import workflow
    from verinoda.store import open_store

    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()


def base_copy(project: str, work: Path, vcopy: Path | None = None) -> Path | None:
    """An indexed git copy of ``project`` at its base commit (made once per work directory)."""
    dst = work / "b" / project
    if (dst / ".verinoda" / "index" / "graph.json").is_file():
        return dst
    if dst.exists():
        _rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if project == "vcopy":
        if vcopy is None:
            return None
        subprocess.run(["git", "-c", "core.longpaths=true", "clone", "-q", "--no-hardlinks", "--", str(vcopy),
                        str(dst)], check=True, capture_output=True)
    else:
        shutil.copytree(EXAMPLES / project, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__"))
        (dst / ".gitignore").write_text("__pycache__/\n*.pyc\n.verinoda/\n", encoding="utf-8")
        _git(dst, "init", "-q")
        _git(dst, "add", "-A")
        _git(dst, "commit", "-q", "-m", "base")
    _index(dst)
    return dst


def apply_edits(repo: Path, edits: list[dict]) -> None:
    for e in edits:
        p = repo / e["file"]
        text = p.read_bytes().decode("utf-8").replace("\r\n", "\n")
        if text.count(e["find"]) != 1:
            raise ValueError(f"{e['file']}: the fixture's find text occurs {text.count(e['find'])} times")
        p.write_bytes(text.replace(e["find"], e["replace"], 1).encode("utf-8"))


# -- matching ---------------------------------------------------------------------------------------------

def _loc(at: str) -> tuple[str, int, int] | None:
    if not at or ":" not in at:
        return None
    f, _, ln = at.rpartition(":")
    a, _, b = ln.partition("-")
    if not a.isdigit():
        return None
    return f, int(a), int(b) if b.isdigit() else int(a)


def _finding_lines(f: dict) -> list[tuple[str, int]]:
    out = []
    for at in [f.get("at"), *(f.get("evidence_at") or [])]:
        loc = _loc(at or "")
        if loc:
            out.append((loc[0], loc[1]))
    return out


def matches(gold: dict, f: dict) -> bool:
    if gold["concern"] != f["concern"]:
        return False
    if not gold.get("at"):
        return True
    g = _loc(gold["at"])
    return g is not None and any(fl == g[0] and g[1] <= ln <= g[2] for fl, ln in _finding_lines(f))


def score(fx: dict, res: dict) -> dict:
    gold = fx["gold"]
    findings = [f for fs in res["concerns"].values() for f in fs]
    per: dict[str, dict] = {}

    def slot(c: str) -> dict:
        return per.setdefault(c, {"tp": 0, "fp": 0, "must": 0, "found": 0, "fp_items": [], "missed": []})

    for f in findings:
        ok = any(matches(g, f) for g in gold["must_find"] + gold["may_find"])
        bad = any(matches(g, f) for g in gold["must_not_flag"])
        if rr.at_least_strong(f["status"]):
            if ok and not bad:
                slot(f["concern"])["tp"] += 1
            else:
                s = slot(f["concern"])
                s["fp"] += 1
                s["fp_items"].append({"at": f["at"], "rule": f["rule"], "status": f["status"],
                                      "finding": f["finding"][:160], "must_not": bad})
    for g in gold["must_find"]:
        s = slot(g["concern"])
        s["must"] += 1
        if any(matches(g, f) for f in findings):
            s["found"] += 1
        else:
            s["missed"].append(g.get("at") or g["concern"])
    # unknowns
    unk = []
    for g in gold["must_say_unknown"]:
        hit = any(u.get("kind") == g["kind"] and (not g.get("at") or u.get("at") == g["at"]) for u in res["unknown"])
        if not hit and g.get("or_bound_at"):
            hit = any((f.get("bound") or {}).get("at") == g["or_bound_at"] for f in findings)
        unk.append({"kind": g["kind"], "at": g.get("at"), "reported": hit})
    # changed symbols
    got = {(c["symbol"], c["kind"]) for c in res["changes"]}
    want = {(c["symbol"], c["kind"]) for c in gold["changes"]}
    changes = {"exact": got == want, "missing": sorted(want - got), "extra": sorted(got - want)}
    # tests
    tests = None
    if gold.get("tests"):
        tg = gold["tests"]
        static = {t["test"] for t in res["tests"].get("static", [])}
        tests = {}
        if tg.get("reach"):
            tests["reach_found"] = len([t for t in tg["reach"] if t in static])
            tests["reach_total"] = len(tg["reach"])
            tests["reach_missing"] = [t for t in tg["reach"] if t not in static]
        if tg.get("no_test_reaches"):
            ntr = set(res["tests"].get("no_test_reaches", []))
            tests["no_test_found"] = len([s for s in tg["no_test_reaches"] if s in ntr])
            tests["no_test_total"] = len(tg["no_test_reaches"])
        ob = res["tests"].get("observe")
        if tg.get("observed_reach") and ob:
            reached = {t for v in (ob.get("reached") or {}).values() for t in v}
            tests["observed_found"] = len([t for t in tg["observed_reach"] if t in reached])
            tests["observed_total"] = len(tg["observed_reach"])
            tests["observed_not_reached_ok"] = all(t in (ob.get("selected_not_reaching") or [])
                                                   for t in tg.get("observed_not_reached", []))
    # affected (dependents and readers of changed bindings)
    dep = {d["symbol"] for d in res["dependents"]} | {r["reader"] for r in res.get("binding_readers", [])
                                                      if r.get("reader")}
    affected = {"found": len([a for a in gold["affected"] if a in dep]), "total": len(gold["affected"]),
                "missing": [a for a in gold["affected"] if a not in dep]}
    # gold locations inside read_first
    rf = []
    for r in res["read_first"]:
        base = r["at"].endswith(" (base)")
        loc = _loc(r["at"].replace(" (base)", ""))
        if loc:
            rf.append((loc, base))
    inside, total = 0, 0
    for g in gold["must_find"]:
        loc = _loc(g.get("at") or "")
        if not loc:
            continue
        total += 1
        base = "base line" in (g.get("note") or "")
        if any(r[0][0] == loc[0] and r[0][1] <= loc[1] and loc[2] <= r[0][2] and (r[1] == base or not base)
               for r in rf) or any(r[0][0] == loc[0] and r[0][1] <= loc[1] <= r[0][2] for r in rf):
            inside += 1
    silent = bool(res["dependents_total"] > len(res["dependents"]) and not res["dependents_truncated"])
    return {"per_concern": per, "unknown": unk, "changes": changes, "tests": tests, "affected": affected,
            "read_first": {"inside": inside, "total": total, "chars": res["budget"]["used_chars"]},
            "silent_truncation": silent}


# -- running ------------------------------------------------------------------------------------------------

def run_fixture(fx: dict, work: Path, *, vcopy: Path | None = None, keep: bool = False) -> dict:
    from verinoda import architecture_map as am
    from verinoda import index
    from verinoda.review import review
    from verinoda.store import open_store

    base = base_copy(fx["project"], work, vcopy)
    if base is None:
        return {"id": fx["id"], "skipped": f"no base copy for {fx['project']} (pass --vcopy)"}
    repo = work / "r" / fx["id"]
    if repo.exists():
        _rmtree(repo)
    shutil.copytree(base, repo)
    try:
        # a copied checkout's index holds the stat data of the originals: refresh it, as any `git status` in a
        # working repository does (otherwise every file looks modified to git's plumbing and is read by content)
        subprocess.run(["git", "update-index", "-q", "--refresh"], cwd=repo, capture_output=True)
        apply_edits(repo, fx["edits"])
        st = open_store(repo)
        try:
            t1 = time.perf_counter()
            review(repo, store=st, record=False)   # as the CLI runs it: the graph is loaded first
            seconds_with_load = time.perf_counter() - t1
            g = index.load(repo)
            t0 = time.perf_counter()   # as the MCP server runs it: the graph is kept in memory
            res = review(repo, store=st, graph=g, max_chars=int(fx["options"].get("max_chars", 6000)))
            seconds = time.perf_counter() - t0
            if fx["options"].get("observe") or fx["options"].get("run_tests"):
                full = review(repo, store=st, graph=g, observe=bool(fx["options"].get("observe")),
                              run_tests=bool(fx["options"].get("run_tests")), record=False)
                res["tests"] = full["tests"]
            impact = am.impact(g, am.changed_files_from_git(repo))
        finally:
            st.close()
        sc = score(fx, res)
        return {"id": fx["id"], "project": fx["project"], "title": fx["title"], "score": sc,
                "seconds": round(seconds, 3), "seconds_with_graph_load": round(seconds_with_load, 3),
                "dependents_total": res["dependents_total"],
                "impact_view_symbols": len(impact.get("affected_symbols") or []),
                "impact_view_capped": len(impact.get("affected_symbols") or []) >= 80,
                "counts": res["counts"], "exit": res["exit"],
                "result": res if keep else None}
    finally:
        if not keep:
            _rmtree(repo)


def aggregate(rows: list[dict]) -> dict:
    per: dict[str, dict] = {}
    unk_hit = unk_total = 0
    ch_exact = 0
    reach_f = reach_t = nt_f = nt_t = aff_f = aff_t = rf_in = rf_t = 0
    silent = 0
    times: dict[str, list[float]] = {}
    ratios = []
    ran = [r for r in rows if "score" in r]
    for r in ran:
        sc = r["score"]
        for c, s in sc["per_concern"].items():
            a = per.setdefault(c, {"tp": 0, "fp": 0, "must": 0, "found": 0})
            for k in a:
                a[k] += s[k]
        unk_hit += sum(1 for u in sc["unknown"] if u["reported"])
        unk_total += len(sc["unknown"])
        ch_exact += int(sc["changes"]["exact"])
        t = sc.get("tests") or {}
        reach_f += t.get("reach_found", 0)
        reach_t += t.get("reach_total", 0)
        nt_f += t.get("no_test_found", 0)
        nt_t += t.get("no_test_total", 0)
        aff_f += sc["affected"]["found"]
        aff_t += sc["affected"]["total"]
        rf_in += sc["read_first"]["inside"]
        rf_t += sc["read_first"]["total"]
        silent += int(sc["silent_truncation"])
        times.setdefault(r["project"], []).append(r["seconds_with_graph_load"])
        if r["impact_view_symbols"]:
            ratios.append({"id": r["id"], "review": r["dependents_total"], "impact_view": r["impact_view_symbols"],
                           "capped": r["impact_view_capped"]})
    for c, a in per.items():
        n = a["tp"] + a["fp"]
        a["precision"] = round(a["tp"] / n, 3) if n else None
        a["recall"] = round(a["found"] / a["must"], 3) if a["must"] else None
    tp = sum(a["tp"] for a in per.values())
    fp = sum(a["fp"] for a in per.values())
    must = sum(a["must"] for a in per.values())
    found = sum(a["found"] for a in per.values())
    return {
        "fixtures": len(rows), "ran": len(ran), "skipped": [r["id"] for r in rows if "skipped" in r],
        "per_concern": per,
        "overall": {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                    "recall": round(found / must, 3) if must else None, "tp": tp, "fp": fp, "must": must,
                    "found": found},
        "must_say_unknown": {"reported": unk_hit, "total": unk_total},
        "changes_exact": {"fixtures": ch_exact, "of": len(ran)},
        "tests": {"static_reach": [reach_f, reach_t], "no_test_reaches": [nt_f, nt_t]},
        "affected": [aff_f, aff_t],
        "read_first_gold_inside": [rf_in, rf_t],
        "silent_truncation": silent,
        "seconds_with_graph_load": {p: {"median": round(statistics.median(v), 3), "max": round(max(v), 3),
                                        "n": len(v)} for p, v in times.items()},
        "blast_radius": ratios,
    }


def evaluate(split: str = "dev", *, work: Path | None = None, vcopy: Path | None = None,
             only: list[str] | None = None, keep: bool = False, progress=None) -> dict:
    data = json.loads((FIXTURES / f"{split}.json").read_text(encoding="utf-8"))
    tmp = None
    if work is None:
        tmp = tempfile.mkdtemp(prefix="verinoda-review-eval-")
        work = Path(tmp)
    work = Path(work)
    rows = []
    try:
        for fx in data["fixtures"]:
            if only and fx["id"] not in only:
                continue
            try:
                row = run_fixture(fx, work, vcopy=vcopy, keep=keep)
            except Exception as exc:  # noqa: BLE001 - one broken fixture is reported, the rest still run
                row = {"id": fx["id"], "error": f"{type(exc).__name__}: {exc}"[:400]}
            rows.append(row)
            if progress:
                progress(row)
    finally:
        if tmp and not keep:
            _rmtree(Path(tmp))
    return {"split": split, "fixtures_sha256": _sha(FIXTURES / f"{split}.json"), "summary": aggregate(rows),
            "rows": rows}


def _sha(p: Path) -> str:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()
