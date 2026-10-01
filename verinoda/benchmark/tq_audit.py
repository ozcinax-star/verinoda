"""The typed-question audit: ``verinoda benchmark tq-audit``.

Runs ``verinoda tq`` (verify on) over the frozen held-out gold sets (``benchmarks/tq_gold/held_out.json`` and
``benchmarks/tq_gold2/held_out.json``, their hashes checked against their MANIFEST.json and against
:data:`verinoda.tq_measured.GOLD_FILES`) and over the dev split of the first set, on fresh indexed copies of the
repositories they name (the first set's query-language fixture and ``examples/orders_app``; this repository at
the pinned commit for the second). Each answer is scored as ``benchmarks/tq_gold/score.py`` scores it (``?`` is
never wrong, a count is right when it is a lower bound) and counted in its cell of (question type, answer,
status).

It writes the calibration table (``verinoda/data/tq_calibration.json``: the gold and engine hashes and every
held-out cell with its Wilson interval, the dev cell beside it and whether ``tq`` may show it) and a report
(``report.json`` with every case, ``report.md``) whose reliability table puts the observed precision of each
status next to ``claims.CONFIDENCE_CAP``. Nothing here changes a cap or an answer.

It needs a source checkout (the gold sets, the fixtures and examples/ are not in the wheel), and the pinned
commit in the checkout's history, else that repository's cases are reported as skipped.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from verinoda import tq_measured as tm

HERE = Path(__file__).resolve().parents[2]
BENCH = HERE / "benchmarks"
HELD_OUT = ("tq_gold/held_out.json", "tq_gold2/held_out.json")
DEV = "tq_gold/dev.json"
# the first set's repositories (its build.py names them; it predates the REPOS form of the second set)
GOLD1_REPOS = {"qlang": {"source": "benchmarks/tq_gold/fixtures/qlang"},
               "orders_app": {"source": "examples/orders_app"}}
STATUSES = ("experiment_verified", "statically_verified", "primary_source_verified", "observed",
            "strong_inference", "weak_inference", "stale")


class GoldError(RuntimeError):
    """A gold file is missing or does not match its frozen hash."""


def _score_mod(bench: Path):
    spec = importlib.util.spec_from_file_location("tq_gold_score", bench / "tq_gold" / "score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_gold(rel: str, bench: Path | None = None) -> tuple[list[dict], str]:
    """The cases of one gold file and its sha256, after checking it against its set's MANIFEST.json."""
    bench = bench or BENCH
    p = bench / rel
    set_dir, name = rel.split("/")
    try:
        data = p.read_bytes()
        manifest = json.loads((bench / set_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoldError(f"{rel}: {type(exc).__name__}: {exc} (a source checkout is needed)") from None
    sha = hashlib.sha256(data).hexdigest()
    if manifest.get("files", {}).get(name) != sha:
        raise GoldError(f"{rel} does not match {set_dir}/MANIFEST.json: the gold set changed after it was frozen")
    if rel in tm.GOLD_FILES and tm.GOLD_FILES[rel] != sha:
        raise GoldError(f"{rel} does not match tq_measured.GOLD_FILES")
    # the code the cases are asked about: the set's fixtures (under the set) and examples (under the repository)
    for key, root in (("fixtures", bench / set_dir), ("examples", bench.parent)):
        for frel, fsha in (manifest.get(key) or {}).items():
            try:
                got = hashlib.sha256((root / frel).read_bytes()).hexdigest()
            except OSError:
                got = None
            if got != fsha:
                raise GoldError(f"{frel} does not match {set_dir}/MANIFEST.json ({key}): the code the gold "
                                "cases are about changed")
    return json.loads(data), sha


def repos_for(rel: str, bench: Path | None = None) -> dict:
    bench = bench or BENCH
    if rel.startswith("tq_gold/"):
        return GOLD1_REPOS
    manifest = json.loads((bench / rel.split("/")[0] / "MANIFEST.json").read_text(encoding="utf-8"))
    return manifest["repos"]


def _index_copy(name: str, spec: dict, work: Path) -> tuple[Path | None, str | None]:
    from verinoda.benchmark import verdict_audit

    return verdict_audit.base_copy(name, spec, work)


def run_cases(cases: list[dict], repos: dict, work: Path, *, split: str, gold: str, score, progress=None) -> list[dict]:
    """tq's answer and the verdict for every case, one batch of at most 20 questions per repository at a time."""
    from verinoda import tq

    by_repo: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        by_repo[c["repo"]].append(c)
    rows: list[dict] = []
    for repo_name in sorted(by_repo):
        cs = by_repo[repo_name]
        spec = repos[repo_name]
        key = hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()[:10]
        base, why = _index_copy(f"{repo_name}-{key}", spec, work)
        if base is None:
            for c in cs:
                rows.append({"id": c["id"], "gold_file": gold, "split": split, "repo": repo_name, "skipped": why})
            continue
        for k in range(0, len(cs), tq.MAX_QUESTIONS):
            part = cs[k:k + tq.MAX_QUESTIONS]
            res = tq.ask(base, [c["question"] for c in part], verify=True)
            for c, a in zip(part, res["answers"]):
                qtype = c["question"].split()[0]
                rows.append({"id": c["id"], "gold_file": gold, "split": split, "repo": repo_name, "type": qtype,
                             "question": c["question"], "gold": c["answer"], "answer": a.get("answer"),
                             "answer_kind": tm.answer_kind({**a, "type": qtype}), "status": a["status"],
                             "at": a.get("at") or [], "why": a.get("why"), "next": a.get("next"),
                             "options": tq.options_of(tq.parse_line(c["question"])),
                             "verdict": score.verdict(a, c), "located": score.located(a, c),
                             "shape": c.get("shape")})
            if progress:
                progress(repo_name, len(rows))
    return rows


def cells_of(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        if r.get("skipped"):
            continue
        key = tm.cell_key(r["type"], r["answer_kind"], r["status"])
        c = out.setdefault(key, {"type": r["type"], "answer": r["answer_kind"], "status": r["status"],
                                 "n": 0, "right": 0, "wrong": 0, "unknown": 0, "options": []})
        c["n"] += 1
        c[r["verdict"]] += 1
        if r.get("options", "") not in c["options"]:
            c["options"] = sorted([*c["options"], r.get("options", "")])
    return out


def _decided(c: dict) -> bool:
    return c["answer"] not in ("?", "invalid")


def calibration(held: list[dict], dev: list[dict]) -> list[dict]:
    """The table's cells: held-out counts, Wilson interval, the dev cell and whether tq may show it."""
    h, d = cells_of(held), cells_of(dev)
    out = []
    for key in sorted(h):
        c = dict(h[key])
        lo, hi = tm.wilson(c["right"], c["n"])
        dc = d.get(key) or {"n": 0, "right": 0}
        c.update({"wilson_lo": round(lo, 4), "wilson_hi": round(hi, 4), "dev_n": dc["n"], "dev_right": dc["right"]})
        dev_ok = dc["n"] > 0 and lo <= dc["right"] / dc["n"] <= hi
        c["dev_inside"] = dev_ok if dc["n"] else None
        c["shown"] = bool(_decided(c) and c["n"] >= tm.MIN_N and dev_ok)
        if not _decided(c):
            c["why_not_shown"] = "not a decided answer"
        elif c["n"] < tm.MIN_N:
            c["why_not_shown"] = f"n < {tm.MIN_N}"
        elif not dc["n"]:
            c["why_not_shown"] = "no dev answer in this cell"
        elif not dev_ok:
            c["why_not_shown"] = "dev precision outside the held-out interval"
        out.append(c)
    return out


def reliability(held: list[dict]) -> list[dict]:
    """Observed precision of decided held-out answers per status, next to claims.CONFIDENCE_CAP."""
    from verinoda.claims import CONFIDENCE_CAP

    out = []
    for st in (*STATUSES, "unknown", "contradicted"):
        rs = [r for r in held if not r.get("skipped") and r["status"] == st and r["answer_kind"] not in ("?", "invalid")]
        k, n = sum(r["verdict"] == "right" for r in rs), len(rs)
        lo, hi = tm.wilson(k, n)
        cap = CONFIDENCE_CAP.get(st)
        row = {"status": st, "cap": cap, "n": n, "right": k}
        if n:
            row.update({"precision": round(k / n, 4), "wilson_lo": round(lo, 4), "wilson_hi": round(hi, 4),
                        "cap_vs_interval": ("inside" if lo <= cap <= hi else "below" if cap < lo else "above")
                        if cap is not None else None})
        out.append(row)
    return out


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_head() -> str | None:
    """The checkout's commit, ``+changes`` when a tracked package file differs from it (the engine hash is exact)."""
    head = (_git("rev-parse", "--short=12", "HEAD") or "").strip()
    if not head:
        return None
    dirty = _git("status", "--porcelain", "--untracked-files=no", "--", "verinoda",
                 ":(exclude)verinoda/data/tq_calibration.json")
    return head + ("+changes" if dirty else "")


def evaluate(*, work: Path | None = None, bench: Path | None = None, held_out: tuple[str, ...] = HELD_OUT,
             dev: str | None = DEV, progress=None) -> dict:
    """Run the audit; ``{"table", "report"}`` (nothing written)."""
    from verinoda.benchmark.review_eval import _rmtree

    bench = bench or BENCH
    score = _score_mod(bench)
    t0 = time.perf_counter()
    eng = tm.engine_sha()
    gold_files: dict[str, str] = {}
    own = work is None
    work = Path(work or tempfile.mkdtemp(prefix="tq-audit-"))
    try:
        held: list[dict] = []
        for rel in held_out:
            cases, sha = load_gold(rel, bench)
            gold_files[rel] = sha
            held += run_cases(cases, repos_for(rel, bench), work, split="held_out", gold=rel, score=score,
                              progress=progress)
        dev_rows: list[dict] = []
        dev_sha = None
        if dev:
            cases, dev_sha = load_gold(dev, bench)
            dev_rows = run_cases(cases, repos_for(dev, bench), work, split="dev", gold=dev, score=score,
                                 progress=progress)
    finally:
        if own:
            _rmtree(work)
    if tm.engine_sha() != eng:
        raise RuntimeError("the engine sources changed while the audit ran; run it again")
    cells = calibration(held, dev_rows)
    table = {"schema": tm.SCHEMA, "written": _dt.date.today().isoformat(), "commit": _git_head(),
             "gold_files": gold_files, "gold_sha": tm.gold_sha(gold_files), "dev_file": {dev: dev_sha} if dev else {},
             "score_sha": tm.scorer_sha(bench),
             "engine": {"sha": eng, "roots": list(tm.ENGINE_ROOTS), "graph_builder": tm.GRAPH_BUILDER + "/**/*.py",
                        "excluded": list(tm.ENGINE_EXCLUDED), "files": tm.engine_files()},
             "min_n": tm.MIN_N, "interval": "Wilson 95%", "verify": True, "cells": cells}
    decided = [r for r in held if not r.get("skipped") and r["answer_kind"] not in ("?", "invalid")]
    summary = {
        "held_out_cases": len(held), "skipped": sum(1 for r in held if r.get("skipped")),
        "decided": len(decided), "right": sum(r["verdict"] == "right" for r in decided),
        "wrong": sum(r["verdict"] == "wrong" for r in decided),
        "unknown": sum(1 for r in held if not r.get("skipped") and r["answer_kind"] in ("?", "invalid")),
        "wrong_at_verified": [r["id"] for r in decided if r["verdict"] == "wrong"
                              and r["status"] in ("statically_verified", "observed", "experiment_verified")],
        "cells": len(cells), "cells_n_ge_30": sum(1 for c in cells if c["n"] >= tm.MIN_N),
        "cells_decided_n_ge_30": sum(1 for c in cells if c["n"] >= tm.MIN_N and _decided(c)),
        "cells_shown": sum(1 for c in cells if c["shown"]),
        "dev_cases": len(dev_rows), "seconds": round(time.perf_counter() - t0, 1),
    }
    report = {"schema": "verinoda.tq_audit/1", "written": table["written"], "commit": table["commit"],
              "gold_files": gold_files, "gold_sha": table["gold_sha"], "engine_sha": eng, "summary": summary,
              "cells": cells, "reliability": reliability(held),
              "wrong": [r for r in held + dev_rows if r.get("verdict") == "wrong"],
              "rows": held + dev_rows}
    return {"table": table, "report": report}


def render_md(report: dict) -> str:
    s = report["summary"]
    out = [f"# tq audit, {report['written']}", "",
           f"Commit {report['commit']}; gold sha256 {report['gold_sha'][:12]} "
           f"({', '.join(report['gold_files'])}); engine sha256 {(report['engine_sha'] or '-')[:12]}.", "",
           f"Held-out: {s['held_out_cases']} cases, {s['skipped']} skipped, {s['decided']} decided "
           f"({s['right']} right, {s['wrong']} wrong), {s['unknown']} unknown; wrong at a verified status: "
           f"{len(s['wrong_at_verified'])}. Dev: {s['dev_cases']} cases.", "",
           f"Cells: {s['cells']}; {s['cells_n_ge_30']} with n >= 30 ({s['cells_decided_n_ge_30']} of them decided); "
           f"{s['cells_shown']} shown by tq as `measured`.", "",
           "## Cells (held-out)", "",
           "| type | answer | status | n | right | wrong | Wilson 95% | dev right/n | shown |",
           "|---|---|---|---|---|---|---|---|---|"]
    for c in report["cells"]:
        out.append(f"| {c['type']} | {c['answer']} | {c['status']} | {c['n']} | {c['right']} | {c['wrong']} | "
                   f"{c['wilson_lo']:.3f}-{c['wilson_hi']:.3f} | {c['dev_right']}/{c['dev_n']} | "
                   f"{'yes' if c['shown'] else 'no: ' + c.get('why_not_shown', '')} |")
    out += ["", "## Reliability against CONFIDENCE_CAP (held-out, decided answers, all types)", "",
            "The caps are not changed here; a cap outside the interval is a finding for a reviewed decision.", "",
            "| status | CONFIDENCE_CAP | n | right | precision | Wilson 95% | cap |", "|---|---|---|---|---|---|---|"]
    for r in report["reliability"]:
        if r["n"]:
            out.append(f"| {r['status']} | {r['cap']} | {r['n']} | {r['right']} | {r['precision']:.3f} | "
                       f"{r['wilson_lo']:.3f}-{r['wilson_hi']:.3f} | {r['cap_vs_interval']} |")
        else:
            out.append(f"| {r['status']} | {r['cap']} | 0 | - | - | - | no answer at this status |")
    out += ["", "## Wrong answers", ""]
    for r in report["wrong"]:
        out.append(f"- `{r['id']}` ({r['split']}) `{r['question']}`: gold {json.dumps(r['gold'])}, answered "
                   f"{json.dumps(r['answer'])} at {r['status']}" + (f"; why: {r['why']}" if r.get("why") else ""))
    if not report["wrong"]:
        out.append("None.")
    return "\n".join(out) + "\n"


def write(res: dict, *, table: Path | None = None, report_dir: Path | None = None) -> dict:
    """Write the table and the report; the paths written."""
    table = Path(table or tm.TABLE)
    report_dir = Path(report_dir or (BENCH / "results" / f"tq-audit-{res['report']['written']}"))
    table.parent.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    table.write_text(json.dumps(res["table"], indent=1) + "\n", encoding="utf-8", newline="\n")
    (report_dir / "report.json").write_text(json.dumps(res["report"], indent=1, default=str) + "\n",
                                            encoding="utf-8", newline="\n")
    (report_dir / "report.md").write_text(render_md(res["report"]), encoding="utf-8", newline="\n")
    return {"table": str(table), "report": str(report_dir / "report.json"), "report_md": str(report_dir / "report.md")}
