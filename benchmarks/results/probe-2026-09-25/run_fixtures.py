"""Run ``verinoda probe`` on the fixtures in fixtures.py and score it against their gold labels.

    python benchmarks/results/probe-2026-09-25/run_fixtures.py WORKDIR OUT.json [--ids D01,E03] [--seeds 5]

WORKDIR receives one git copy of examples/orders_app per fixture (EXTRA committed as the base, the edits left in
the working tree). Before probing, the fixture's own tests are run on the working tree: a behaviour change or an
equivalent edit must keep them passing (the measurement is about what the tests miss). Scores:

* detection: ``change`` fixtures with status differences_found/property_violated and a gold class, plus the
  ``scaling`` fixture's growth flag;
* false alarms: any difference (numeric drift included) on an ``equivalent`` fixture, per seed; any difference
  on the scaling fixture run without scaling;
* gate: ``refuse`` fixtures refused with the gold sink kind named (correct refusals), ``run`` fixtures refused
  (incorrect refusals);
* ``unsupported`` fixtures with that status;
* reproducibility: every reported class reproduced in the confirmation runs;
* time per probe (the probe's own duration_s: gate, corpus, every run).
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import fixtures  # noqa: E402

from verinoda import probe  # noqa: E402
from verinoda.store import open_store  # noqa: E402

CHANGED = ("differences_found", "property_violated")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def _rmtree(path: Path) -> None:
    for p in path.rglob("*"):  # git object files are read-only on Windows
        try:
            p.chmod(0o666 if p.is_file() else 0o777)
        except OSError:
            pass
    shutil.rmtree(path)


def build(work: Path, fx: dict) -> Path:
    d = work / fx["id"]
    if d.exists():
        _rmtree(d)
    shutil.copytree(ROOT / "examples" / "orders_app", d,
                    ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    for rel, text in fixtures.EXTRA.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    _git(d, "init", "-q")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "base")
    for rel, old, new in fx.get("edits") or []:
        p = d / rel
        t = p.read_text(encoding="utf-8")
        assert old in t, (fx["id"], rel, old)
        p.write_bytes(t.replace(old, new, 1).encode("utf-8"))
    return d


def tests_pass(d: Path) -> bool:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=d,
                       capture_output=True, text=True, timeout=300)
    return r.returncode == 0


def one(d: Path, fx: dict, seed: int, **over) -> dict:
    opts = {**(fx.get("options") or {}), **over}
    st = open_store(d)
    t0 = time.monotonic()
    try:
        res = probe.probe(st, d, fx["target"], seed=seed, **opts)
    except ValueError as exc:
        res = {"status": "error", "headline": str(exc)}
    finally:
        st.close()
    wall = round(time.monotonic() - t0, 3)
    diffs = res.get("differences") or []
    return {"status": res.get("status"), "headline": res.get("headline"), "classes": [x["class"] for x in diffs],
            "examples": {x["class"]: (x["examples"][0]["call"] if x.get("examples") else None) for x in diffs},
            "reproduced": [x.get("reproduced") for x in diffs if x["class"] != "growth_changed"],
            "gate": {"verdict": (res.get("gate") or {}).get("verdict"),
                     "reasons": [{"kind": r["kind"], "at": r["at"]} for r in (res.get("gate") or {}).get("reasons", [])[:6]]},
            "inputs": (res.get("inputs") or {}).get("count"), "generator": (res.get("inputs") or {}).get("generator"),
            "scaling": (res.get("scaling") or {}).get("verdict"), "undeclared": [u["type"] for u in
                                                                                  res.get("undeclared_exceptions") or []],
            "duration_s": res.get("duration_s"), "wall_s": wall, "seed": seed, "options": opts,
            "claims": [x.get("claim_status") for x in diffs if x.get("claim_id")]}


def score(fx: dict, runs: list[dict]) -> dict:
    g = fx["gold"]
    r = runs[0]
    if g == "change":
        ok = r["status"] in CHANGED and bool(set(r["classes"]) & set(fx["classes"]))
        return {"detected": ok}
    if g == "equivalent":
        alarms = [x for x in runs if x["classes"] or x["status"] in CHANGED]
        return {"false_alarms": len(alarms), "seeds": len(runs),
                "not_clean": [x["status"] for x in runs if x["status"] != "no_difference_found"]}
    if g == "refuse":
        kinds = {x["kind"] for x in r["gate"]["reasons"]}
        at_ok = fx.get("at") is None or any(x["at"] == fx["at"] for x in r["gate"]["reasons"])
        return {"refused": r["status"] == "refused", "sink_named": fx["sink"] in kinds, "at_named": at_ok}
    if g == "run":
        return {"incorrectly_refused": r["status"] == "refused"}
    if g == "unsupported":
        return {"unsupported": r["status"] == "unsupported"}
    if g == "scaling":
        flagged = "growth_changed" in r["classes"]
        quiet = [x for x in runs[1:] if x["classes"] or x["status"] in CHANGED]
        return {"flagged": flagged, "false_alarms_without_scaling": len(quiet)}
    return {}


def main(argv: list[str]) -> int:
    work, out = Path(argv[0]), Path(argv[1])
    ids = None
    seeds = 5
    for i, a in enumerate(argv):
        if a == "--ids":
            ids = set(argv[i + 1].split(","))
        if a == "--seeds":
            seeds = int(argv[i + 1])
    if "--no-hypothesis" in argv:  # the fallback generator: a fixed pseudo-random list
        sys.modules["hypothesis"] = None
    if "--no-mining" in argv:  # ablation: no boundaries mined from the code (edges and generated values only)
        probe.pin.mine = lambda fn, rel, lookup, bounds, define=None: bounds
    work.mkdir(parents=True, exist_ok=True)
    gold_sha = hashlib.sha256((HERE / "fixtures.py").read_bytes()).hexdigest()
    rows = []
    for fx in fixtures.FIXTURES:
        if ids and fx["id"] not in ids:
            continue
        d = build(work, fx)
        passing = tests_pass(d) if fx["gold"] in ("change", "equivalent", "scaling") else None
        runs = []
        if fx["gold"] == "equivalent":
            runs = [one(d, fx, s) for s in range(seeds)]
        elif fx["gold"] == "scaling":
            runs = [one(d, fx, 0), one(d, fx, 0, scaling=False)]
        else:
            runs = [one(d, fx, 0)]
        row = {"id": fx["id"], "kind": fx["kind"], "gold": fx["gold"], "target": fx["target"],
               "tests_pass_on_working_tree": passing, "score": score(fx, runs), "runs": runs}
        rows.append(row)
        print(json.dumps({"id": fx["id"], "gold": fx["gold"], "status": [r["status"] for r in runs],
                          "classes": runs[0]["classes"], "score": row["score"],
                          "s": [r["duration_s"] for r in runs]}, ensure_ascii=True), flush=True)
    summary = summarise(rows)
    res = {"gold_sha256": gold_sha, "python": platform.python_version(), "platform": sys.platform,
           "hypothesis": "off" if "--no-hypothesis" in argv else "on when installed",
           "boundary_mining": "off (ablation)" if "--no-mining" in argv else "on",
           "summary": summary, "fixtures": rows}
    text = json.dumps(res, indent=1, ensure_ascii=True)
    text = text.replace(str(work).replace("\\", "\\\\"), "<WORK>").replace(str(ROOT).replace("\\", "\\\\"), "<REPO>")
    out.write_text(text, encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def summarise(rows: list[dict]) -> dict:
    by = lambda g: [r for r in rows if r["gold"] == g]  # noqa: E731
    change, eq, ref, run_, uns, sc = (by(g) for g in ("change", "equivalent", "refuse", "run", "unsupported",
                                                        "scaling"))
    det = sum(r["score"]["detected"] for r in change) + sum(r["score"]["flagged"] for r in sc)
    times = sorted(x["duration_s"] for r in rows for x in r["runs"] if x.get("duration_s") is not None
                   and x["status"] not in ("refused", "unsupported"))
    reproduced = [v for r in rows for x in r["runs"] for v in x["reproduced"]]
    surviving = [r for r in change + sc if r["tests_pass_on_working_tree"]]
    det_surviving = sum(r["score"].get("detected", False) or r["score"].get("flagged", False) for r in surviving)
    return {
        "detection": f"{det}/{len(change) + len(sc)}",
        "missed": [r["id"] for r in change if not r["score"]["detected"]] + [r["id"] for r in sc
                                                                            if not r["score"]["flagged"]],
        "false_alarms_equivalent": f"{sum(r['score']['false_alarms'] for r in eq)} of "
                                   f"{sum(r['score']['seeds'] for r in eq)} runs ({len(eq)} fixtures)",
        "false_alarm_ids": [r["id"] for r in eq if r["score"]["false_alarms"]],
        "equivalent_not_clean": {r["id"]: r["score"]["not_clean"] for r in eq if r["score"]["not_clean"]},
        "false_alarms_scaling_off": sum(r["score"]["false_alarms_without_scaling"] for r in sc),
        "refusals_correct": f"{sum(r['score']['refused'] and r['score']['sink_named'] for r in ref)}/{len(ref)}",
        "refusal_misses": [r["id"] for r in ref if not (r["score"]["refused"] and r["score"]["sink_named"])],
        "refusals_incorrect": f"{sum(r['score']['incorrectly_refused'] for r in run_)}/{len(run_)}",
        "unsupported_correct": f"{sum(r['score']['unsupported'] for r in uns)}/{len(uns)}",
        "caught_by_existing_tests": [r["id"] for r in rows if r["tests_pass_on_working_tree"] is False],
        "detection_on_test_surviving": f"{det_surviving}/{len(surviving)}",
        "reproduced": f"{sum(1 for v in reproduced if v)}/{len(reproduced)}",
        "time_per_probe_s": {"median": round(statistics.median(times), 2) if times else None,
                             "p90": round(times[int(0.9 * (len(times) - 1))], 2) if times else None,
                             "max": round(times[-1], 2) if times else None, "n": len(times)},
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
