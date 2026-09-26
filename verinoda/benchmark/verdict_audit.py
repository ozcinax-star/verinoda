"""The verdict audit (docs/DESIGN.md D-honest-verdicts): ``verinoda benchmark verdict-audit``.

``benchmarks/verdict_audit/cases.json`` holds questions where ``verinoda analyze`` said ``met`` for an
irrelevant or incomplete answer (*traps*) and questions where ``met`` is right (*controls*), on public
material only: ``examples/``, small fixtures under ``tests/fixtures/verdict_audit/`` and this repository at
a pinned commit. Each project is copied once into a work directory, committed and indexed; every run
copies that base again, so no run reuses claims of an earlier one. Nothing is written into the repository.

A case's verdict is the verdict of its sub-questions taken together: ``met`` when all are met,
``met_with_inference`` when at least one is met or met with inference, otherwise the first one's.
Its *answer* is the text and evidence of the claims its sub-questions name as answering.

- ``wrong_met``: the verdict is ``met`` but the answer misses a ``gold.must`` string, holds a
  ``gold.must_not`` string, or the case says no answer can make ``met`` right (``gold.met_is_wrong``: the
  true answer is an absence the tool cannot state). Strings are matched case-insensitively.
- ``above_ceiling``: the verdict is above the case's ``ceiling`` (``met`` > ``met_with_inference`` >
  ``not_met``, which is every other verdict). A ``met`` above the ceiling with a complete answer means the
  case's ceiling needs review, not that the verdict is wrong.
- ``controls_kept``: controls judged ``met`` with a complete answer (over-refusal is what they measure).

It needs a source checkout (the cases, examples/ and the fixtures are not in the wheel); the ``verinoda``
project also needs the pinned commit in the checkout's history, else its cases are reported as skipped.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
CASES = HERE / "benchmarks" / "verdict_audit" / "cases.json"
LEVEL = {"met": 3, "met_with_inference": 2}           # every other verdict: 1 (not answered)
CEILING = {"met": 3, "met_with_inference": 2, "not_met": 1}
SPLITS = ("dev", "held_out")


def load_cases(path: Path | None = None) -> dict:
    return json.loads(Path(path or CASES).read_text(encoding="utf-8"))


def level(verdict: str | None) -> int:
    return LEVEL.get(verdict or "", 1)


def overall(verdicts: list[str]) -> str:
    """One verdict for a question from its sub-questions' verdicts."""
    if not verdicts:
        return "none"
    if all(v == "met" for v in verdicts):
        return "met"
    if any(v in LEVEL for v in verdicts):
        return "met_with_inference"
    return verdicts[0]


def answer_text(res: dict) -> str:
    """Text and evidence of the claims the sub-questions name as answering (one line per claim)."""
    by_id = {c["id"]: c for c in res.get("claims") or []}
    ids = [cid for s in res.get("subquestions") or [] for cid in s.get("answer_claim_ids") or []]
    lines = []
    for cid in dict.fromkeys(ids):
        c = by_id.get(cid)
        if c is not None:
            lines.append(" ".join([c.get("text") or "", *[str(e) for e in c.get("evidence") or []]]))
    return "\n".join(lines)


def gold_check(gold: dict, text: str) -> dict:
    low = text.lower()
    missing = [m for m in gold.get("must") or [] if m.lower() not in low]
    wrong = [m for m in gold.get("must_not") or [] if m.lower() in low]
    ok = not missing and not wrong and not gold.get("met_is_wrong")
    return {"complete": ok, "missing": missing, "wrong": wrong}


def score_case(case: dict, verdict: str, text: str) -> dict:
    g = gold_check(case.get("gold") or {}, text)
    met = verdict == "met"
    return {"verdict": verdict, "wrong_met": met and not g["complete"], "right_met": met and g["complete"],
            "above_ceiling": level(verdict) > CEILING[case["ceiling"]], "gold": g}


# -- project copies -------------------------------------------------------------------------------------------

def _index(root: Path, config: dict | None) -> None:
    from verinoda import workflow
    from verinoda.paths import atlas_dir
    from verinoda.store import open_store

    workflow.init(root)
    if config:
        p = atlas_dir(root) / "config.json"
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        for k, v in config.items():
            cfg[k] = {**cfg.get(k, {}), **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
        p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()


def base_copy(name: str, spec: dict, work: Path) -> tuple[Path | None, str | None]:
    """``(indexed copy, None)`` of project ``name`` (made once per work directory), or ``(None, why)``."""
    from verinoda.benchmark import approaches as ap
    from verinoda.benchmark.review_eval import _rmtree

    dst = work / "base" / name
    if (dst / ".verinoda" / "index" / "graph.json").is_file():
        return dst, None
    if dst.exists():
        _rmtree(dst)
    src = (HERE / spec["source"]).resolve()
    if not src.exists():
        return None, f"{spec['source']} not found (a source checkout is needed)"
    try:
        ap.prepare_workdir(src, dst, include=spec.get("include"), exclude=spec.get("exclude"),
                           commit=spec.get("git_commit"))
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        _rmtree(dst)
        return None, f"{type(exc).__name__}: {exc}"[:300]
    _index(dst, spec.get("verinoda_config"))
    return dst, None


# -- running ----------------------------------------------------------------------------------------------------

def run_case(case: dict, repo: Path, store) -> dict:
    from verinoda import analysis

    t0 = time.perf_counter()
    res = analysis.analyze(store, repo, case["question"])
    secs = round(time.perf_counter() - t0, 2)
    subs = res.get("subquestions") or []
    verdict = overall([s.get("status") or "?" for s in subs])
    text = answer_text(res)
    row = {"id": case["id"], "split": case["split"], "kind": case["kind"], "shape": case.get("shape"),
           "project": case["project"], "question": case["question"], "ceiling": case["ceiling"],
           "subquestions": [{"id": s["id"], "intent": s.get("intent"), "status": s.get("status")} for s in subs],
           **score_case(case, verdict, text), "answer": text[:1500], "seconds": secs,
           "unknowns": [u.get("why", "")[:240] for u in res.get("unknowns") or []][:8]}
    return row


def summarize(rows: list[dict]) -> dict:
    out = {}
    for split in (*SPLITS, "all"):
        rs = [r for r in rows if (split == "all" or r["split"] == split) and not r.get("skipped")]
        traps = [r for r in rs if r["kind"] == "trap"]
        controls = [r for r in rs if r["kind"] == "control"]
        wrong = [r for r in rs if r["wrong_met"]]
        kept = [r for r in controls if r["right_met"]]
        out[split] = {
            "cases": len(rs), "traps": len(traps), "controls": len(controls),
            "wrong_met": len(wrong), "wrong_met_rate": round(len(wrong) / len(rs), 3) if rs else None,
            "wrong_met_traps": sum(1 for r in traps if r["wrong_met"]),
            "wrong_met_controls": sum(1 for r in controls if r["wrong_met"]),
            "above_ceiling": sum(1 for r in rs if r["above_ceiling"]),
            "controls_kept": len(kept), "controls_kept_rate": round(len(kept) / len(controls), 3) if controls else None,
            "verdicts": dict(sorted(Counter(r["verdict"] for r in rs).items())),
            "wrong_met_ids": [r["id"] for r in wrong],
            "controls_lost_ids": [r["id"] for r in controls if not r["right_met"]],
            "skipped": sum(1 for r in rows if r.get("skipped") and (split == "all" or r["split"] == split)),
        }
    return out


def evaluate(split: str = "dev", *, work: Path | None = None, only: list[str] | None = None,
             cases_path: Path | None = None, progress=None) -> dict:
    """Run the cases of ``split`` (``dev``, ``held_out`` or ``all``); ``{"rows", "summary"}``."""
    from verinoda.benchmark.review_eval import _rmtree
    from verinoda.store import open_store

    data = load_cases(cases_path)
    cases = [c for c in data["cases"] if (split == "all" or c["split"] == split) and (not only or c["id"] in only)]
    own = work is None
    work = Path(work or tempfile.mkdtemp(prefix="verdict-audit-"))
    run_dir = work / f"run-{int(time.time() * 1000)}"
    rows: list[dict] = []
    try:
        by_project: dict[str, list[dict]] = {}
        for c in cases:
            by_project.setdefault(c["project"], []).append(c)
        for name, pcases in by_project.items():
            base, why = base_copy(name, data["projects"][name], work)
            if base is None:
                for c in pcases:
                    rows.append({"id": c["id"], "split": c["split"], "kind": c["kind"], "project": name,
                                 "skipped": why})
                    if progress:
                        progress(rows[-1])
                continue
            repo = run_dir / name
            shutil.copytree(base, repo)
            store = open_store(repo)
            try:
                for c in pcases:
                    try:
                        rows.append(run_case(c, repo, store))
                    except Exception as exc:  # noqa: BLE001 - one broken case is reported, the rest still run
                        rows.append({"id": c["id"], "split": c["split"], "kind": c["kind"], "project": name,
                                     "skipped": f"error: {type(exc).__name__}: {exc}"[:300]})
                    if progress:
                        progress(rows[-1])
            finally:
                store.close()
                _rmtree(repo)
    finally:
        _rmtree(run_dir)
        if own:
            _rmtree(work)
    order = {c["id"]: i for i, c in enumerate(data["cases"])}
    rows.sort(key=lambda r: order.get(r["id"], 0))
    return {"schema": "verinoda.verdict_audit_result/1", "split": split, "rows": rows, "summary": summarize(rows)}
