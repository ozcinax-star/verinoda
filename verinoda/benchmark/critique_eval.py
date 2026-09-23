"""Critique evaluation (docs/DESIGN.md D30): does critique flag false claims and leave true ones alone?

A labelled set of claims about ``examples/orders_app`` (every one citing real
lines of the example):

* true claims: call relations read off the code (plain calls, a constructor, two
  method calls), exact definition spans, environment reads, a static test-reach
  claim, an exclusivity claim, ADR attribution and two plain-text claims;
* false claims: the benchmark's own negatives for this corpus
  (``verinoda/benchmark/questions/orders_app.json``, e.g. ``q07.n.reversed``,
  ``q04.n.empty_order``, ``q03.n.repo_env``), plus mutation operators applied
  to true claims - direction swap, target substitution, off-by-N spans, a wrong
  environment variable, an "only X" claim with a planted second occurrence, a
  negated or altered plain-text claim, a misattributed decision and the
  acceptance audit's "Orders are stored in PostgreSQL".

Each claim is created through :class:`verinoda.claims.Claims` with the status
a careless producer would request (the verified one), so two layers are
measured: the *entailment gate* (does irrelevant or non-stating evidence reach
a verified status?) and *critique* (:func:`verinoda.critique.challenge`).

Reported: per-case outcome; for false claims the flag rate (status lowered or
contradicted) and refutation rate; for true claims the false-alarm and false
refutation rates; precision of the statuses presented as verified; per-check
firing counts; each with a Wilson 95% interval.

``python -m verinoda.benchmark.critique_eval [--out FILE]``
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from verinoda import evidence as evmod
from verinoda.claims import ORDER, VERIFIED, Claims

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "orders_app"
_RANK = {s: i for i, s in enumerate(ORDER)}


def _rank(status: str) -> int:
    return _RANK.get(status, len(ORDER))


def _wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return [round((c - r) / d, 4), round((c + r) / d, 4)]


def _rel(cid: str, truth: bool, caller: str, callee: str, at: str, callee_file: str, *, source: str,
         confidence: str = "EXTRACTED") -> dict:
    path = at.rpartition(":")[0]
    return {"id": cid, "truth": truth, "source": source, "kind": "relation",
            "text": f"`{caller}` calls `{callee}` ({at})", "evidence": [(at, None)],
            "subjects": [f"{path}::{caller}", f"{callee_file}::{callee}"],
            "spec": {"target_label": callee, "relation": "calls", "at": at, "confidence": confidence}}


def _loc(cid: str, truth: bool, sym: str, path: str, a: int, b: int, *, source: str) -> dict:
    return {"id": cid, "truth": truth, "source": source, "kind": "location",
            "text": f"`{sym}` is defined at {path}:{a}-{b}", "evidence": [(f"{path}:{a}-{b}", {"symbol": sym})],
            "subjects": [f"{path}::{sym}"], "spec": {}}


def _cfg(cid: str, truth: bool, var: str, at: str, symbol: str, *, source: str) -> dict:
    path = at.rpartition(":")[0]
    return {"id": cid, "truth": truth, "source": source, "kind": "config",
            "text": f"`{symbol}` in {path} reads environment variable {var} ({at})", "evidence": [(at, {"env": var})],
            "subjects": [path], "spec": {"env": var}}


def _gen(cid: str, truth: bool, text: str, at: str, *, source: str, kind: str = "general",
         stype: str = "source_code", spec: dict | None = None, subjects: list[str] | None = None) -> dict:
    return {"id": cid, "truth": truth, "source": source, "kind": kind, "text": text, "evidence": [(at, None)],
            "stype": stype, "subjects": subjects or [], "spec": spec or {}}


ADR = "docs/adr/0001-sqlite-persistence.md"
REACH = "Test code statically reaches `apply_discount` from: "


def cases() -> list[dict]:
    """The labelled set (line numbers refer to examples/orders_app as shipped)."""
    return [
        # -- relations: true ---------------------------------------------------------------------
        _rel("t.handler_place", True, "create_order_handler()", "place_order()", "orders/api.py:18",
             "orders/service.py", source="gold q02.handler"),
        _rel("t.handler_repo", True, "create_order_handler()", "get_repo()", "orders/api.py:18", "orders/api.py",
             source="code"),
        _rel("t.get_fetch", True, "get_order_handler()", "fetch_order()", "orders/api.py:25", "orders/service.py",
             source="gold q08.handler"),
        _rel("t.place_validate", True, "place_order()", "validate_items()", "orders/service.py:20",
             "orders/service.py", source="code"),
        _rel("t.place_total", True, "place_order()", "compute_total()", "orders/service.py:21", "orders/pricing.py",
             source="code"),
        _rel("t.total_discount", True, "compute_total()", "apply_discount()", "orders/pricing.py:8",
             "orders/pricing.py", source="gold q07.caller"),
        _rel("t.repo_ctor", True, "get_repo()", "OrderRepository", "orders/api.py:12", "orders/repository.py",
             source="code"),
        _rel("t.place_save", True, "place_order()", ".save()", "orders/service.py:22", "orders/repository.py",
             source="gold q02.service", confidence="INFERRED"),
        _rel("t.fetch_get", True, "fetch_order()", ".get()", "orders/service.py:26", "orders/repository.py",
             source="gold q08.service", confidence="INFERRED"),
        _rel("t.test_total", True, "test_compute_total()", "compute_total()", "tests/test_pricing.py:13",
             "orders/pricing.py", source="code"),
        # -- relations: false (benchmark negatives, then mutations) ------------------------------
        _rel("f.handler_save", False, "create_order_handler()", ".save()", "orders/api.py:18",
             "orders/repository.py", source="negative q02.n.handler_save"),
        _rel("f.place_discount", False, "place_order()", "apply_discount()", "orders/service.py:21",
             "orders/pricing.py", source="negative q07.n.service_discount"),
        _rel("f.reversed", False, "apply_discount()", "compute_total()", "orders/pricing.py:8", "orders/pricing.py",
             source="negative q07.n.reversed"),
        _rel("f.fetch_save", False, "fetch_order()", ".save()", "orders/service.py:26", "orders/repository.py",
             source="negative q08.n.fetch_save"),
        _rel("f.get_place", False, "get_order_handler()", "place_order()", "orders/api.py:25", "orders/service.py",
             source="negative q08.n.handler_place"),
        _rel("f.handler_validate", False, "create_order_handler()", "validate_items()", "orders/api.py:18",
             "orders/service.py", source="negative q10.n.handler_validates"),
        _rel("f.get_save", False, "get_order_handler()", ".save()", "orders/api.py:25", "orders/repository.py",
             source="negative q06.n.read_path"),
        _rel("f.swap", False, "validate_items()", "place_order()", "orders/service.py:20", "orders/service.py",
             source="mutation direction_swap"),
        _rel("f.substitute", False, "place_order()", "fetch_order()", "orders/service.py:21", "orders/service.py",
             source="mutation target_substitution"),
        _rel("f.service_sql", False, "place_order()", "connect()", "orders/service.py:22", "orders/repository.py",
             source="negative q01.n.service_sql"),
        # -- locations -----------------------------------------------------------------------------
        _loc("t.loc_total", True, "compute_total()", "orders/pricing.py", 6, 8, source="code"),
        _loc("t.loc_discount", True, "apply_discount()", "orders/pricing.py", 11, 15, source="gold q07.apply"),
        _loc("t.loc_place", True, "place_order()", "orders/service.py", 19, 22, source="code"),
        _loc("t.loc_save", True, ".save()", "orders/repository.py", 15, 20, source="gold q01.save"),
        _loc("t.loc_adr", True, "ADR 0001: SQLite for order persistence", ADR, 1, 7, source="code"),
        _loc("f.loc_total_long", False, "compute_total()", "orders/pricing.py", 6, 9, source="mutation off_by_1"),
        _loc("f.loc_place_long", False, "place_order()", "orders/service.py", 19, 25, source="mutation off_by_3"),
        _loc("f.loc_place_short", False, "place_order()", "orders/service.py", 19, 21, source="mutation off_by_-1"),
        _loc("f.loc_wrong_start", False, "compute_total()", "orders/pricing.py", 5, 8, source="mutation start-1"),
        # -- config ----------------------------------------------------------------------------------
        _cfg("t.env_db", True, "ORDERS_DATABASE_URL", "orders/config.py:5", "config.py", source="gold q03.db"),
        _cfg("t.env_max", True, "ORDERS_MAX_ITEMS", "orders/config.py:6", "config.py", source="gold q03.max"),
        _cfg("t.env_discount", True, "ORDERS_DISCOUNT_THRESHOLD", "orders/config.py:7", "config.py",
             source="gold q03.discount"),
        _cfg("f.env_wrong_var", False, "ORDERS_MAX_ITEMS", "orders/config.py:5", "config.py",
             source="mutation wrong_variable"),
        _cfg("f.repo_env", False, "ORDERS_DATABASE_URL", "orders/repository.py:9", "OrderRepository.__init__",
             source="negative q03.n.repo_env"),
        # -- tests reach ------------------------------------------------------------------------------
        _gen("t.reach", True, REACH + "test_compute_total, test_place_and_fetch_roundtrip", "tests/test_pricing.py:12",
             source="gold q04", kind="tests", subjects=["orders/pricing.py::apply_discount()"],
             spec={"tests": ["tests/test_pricing.py::test_compute_total",
                             "tests/test_service.py::test_place_and_fetch_roundtrip"]}),
        _gen("f.reach_empty", False, REACH + "test_compute_total, test_empty_order_rejected",
             "tests/test_service.py:13", source="negative q04.n.empty_order", kind="tests",
             subjects=["orders/pricing.py::apply_discount()"],
             spec={"tests": ["tests/test_pricing.py::test_compute_total",
                             "tests/test_service.py::test_empty_order_rejected"]}),
        # -- exclusivity (the false one is evaluated after a second connection is planted) -------------
        _gen("t.only_repo", True, "Only orders/repository.py opens a database connection", "orders/repository.py:10",
             source="gold q09.connect", kind="exclusive", subjects=["orders/repository.py"],
             spec={"pattern": r"sqlite3\.connect\(", "allowed_files": ["orders/repository.py"]}),
        # -- documents ---------------------------------------------------------------------------------
        _gen("t.adr", True, f"Decision record {ADR} (status: accepted) explains it: We persist orders in SQLite "
             "through `OrderRepository` only. No other module", f"{ADR}:1-7", source="gold q05", kind="decision",
             stype="design_doc", subjects=[ADR]),
        _gen("f.adr_postgres", False, f"Decision record {ADR} (status: accepted) explains it: We persist orders in "
             "PostgreSQL", f"{ADR}:1-7", source="mutation misattribution", kind="decision", stype="design_doc",
             subjects=[ADR]),
        # -- plain text ----------------------------------------------------------------------------------
        _gen("t.text_discount", True, "compute_total applies the discount", "orders/pricing.py:6-8", source="code"),
        _gen("t.text_threshold", True, "the discount threshold is read from ORDERS_DISCOUNT_THRESHOLD",
             "orders/config.py:7", source="gold q07.threshold"),
        _gen("f.text_postgres", False, "Orders are stored in PostgreSQL", "orders/config.py:5",
             source="audit criterion 6"),
        _gen("f.text_negated", False, "compute_total does not apply the discount", "orders/pricing.py:6-8",
             source="mutation negation"),
        _gen("f.text_factor", False, "apply_discount multiplies the subtotal by 0.8", "orders/pricing.py:14",
             source="mutation constant"),
    ]


PLANTED = [("f.only_repo_planted", "orders/audit.py",
            '"""Audit trail."""\nimport sqlite3\n\n\ndef log(msg):\n    conn = sqlite3.connect("audit.db")\n'
            '    conn.execute("INSERT INTO log VALUES (?)", (msg,))\n')]


def _git_init(repo: Path) -> None:
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "init")):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                       cwd=repo, check=True, capture_output=True, stdin=subprocess.DEVNULL)


def _create(cl: Claims, repo: Path, snap: dict, case: dict) -> dict:
    evs = []
    for loc, meta in case["evidence"]:
        path, _, rng = loc.rpartition(":")
        a, _, b = rng.partition("-")
        ev = evmod.source_evidence(repo, path, int(a), int(b) if b else None, commit=snap["commit_sha"],
                                   source_type=case.get("stype", "source_code"), meta=dict(meta or {}))
        if ev is None:
            raise ValueError(f"{case['id']}: {loc} is not citable")
        evs.append((ev, "supports"))
    requested = "primary_source_verified" if case["kind"] == "decision" else \
        "strong_inference" if case["kind"] == "tests" else "statically_verified"
    return cl.create(case["text"], project=snap["project"], snapshot=snap, status=requested, evidence=evs,
                     kind=case["kind"], spec=case["spec"], subjects=case["subjects"], actor="critique_eval")


def evaluate(example: Path | None = None, workdir: Path | None = None) -> dict:
    """Create every labelled claim on a scanned copy of the example and challenge it."""
    from verinoda import critique, index, workflow
    from verinoda.store import open_store

    example = Path(example or EXAMPLE)
    base = Path(workdir or tempfile.mkdtemp(prefix="ra_critique_eval_"))
    repo = base / "orders_app"
    shutil.copytree(example, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    rows: list[dict] = []
    try:
        _git_init(repo)
        st = open_store(repo)
        try:
            workflow.scan(st, repo)
            g = index.load(repo)
            snap = st.latest_snapshot()
            cl = Claims(st, repo)
            todo = [(c, None) for c in cases()]
            only = next(c for c in cases() if c["id"] == "t.only_repo")
            todo += [({**only, "id": pid, "truth": False, "source": "mutation planted_second_occurrence"},
                      (rel, text)) for pid, rel, text in PLANTED]
            for case, plant in todo:
                if plant:
                    (repo / plant[0]).write_bytes(plant[1].encode("utf-8"))
                c = _create(cl, repo, snap, case)
                created = c["status"]
                t0 = time.perf_counter()
                res = critique.challenge(st, repo, c["id"], graph=g)
                ms = round(1000 * (time.perf_counter() - t0), 1)
                after = res["after"]["status"]
                fired = sorted({f["check"] for f in res["findings"] if f["result"] in ("fail", "warn")})
                rows.append({"id": case["id"], "truth": case["truth"], "source": case["source"],
                             "kind": case["kind"], "created": created, "after": after,
                             "lowered": _rank(after) > _rank(created), "contradicted": after == "contradicted",
                             "fired": fired, "ms": ms})
        finally:
            st.close()
    finally:
        if workdir is None:
            shutil.rmtree(base, ignore_errors=True)
    return summarize(rows)


def summarize(rows: list[dict]) -> dict:
    true = [r for r in rows if r["truth"]]
    false = [r for r in rows if not r["truth"]]

    def rate(rs: list[dict], key) -> dict:
        k = sum(1 for r in rs if key(r))
        return {"k": k, "n": len(rs), "rate": round(k / len(rs), 4) if rs else None, "wilson95": _wilson(k, len(rs))}

    presented = [r for r in rows if r["after"] in VERIFIED]
    strong = [r for r in rows if r["after"] == "strong_inference"]
    fired_true, fired_false = Counter(), Counter()
    for r in rows:
        (fired_true if r["truth"] else fired_false).update(r["fired"])
    negatives = [r for r in false if r["source"].startswith("negative")]
    ms = sorted(r["ms"] for r in rows)
    return {
        "schema": "verinoda.critique_eval/1", "cases": len(rows), "true": len(true), "false": len(false),
        "gate": {  # the entailment gate alone: status at creation
            "false_verified_at_creation": rate(false, lambda r: r["created"] in VERIFIED),
            "true_verified_at_creation": rate(true, lambda r: r["created"] in VERIFIED),
        },
        "critique": {
            "false_flagged": rate(false, lambda r: r["lowered"] or r["contradicted"] or r["created"] not in VERIFIED),
            "false_contradicted": rate(false, lambda r: r["contradicted"]),
            "true_false_alarm": rate(true, lambda r: r["lowered"]),
            "true_contradicted": rate(true, lambda r: r["contradicted"]),
        },
        "presented_as_verified": {"n": len(presented), "true": sum(1 for r in presented if r["truth"]),
                                  "precision": round(sum(1 for r in presented if r["truth"]) / len(presented), 4)
                                  if presented else None,
                                  "wilson95": _wilson(sum(1 for r in presented if r["truth"]), len(presented))},
        "presented_as_strong_inference": {"n": len(strong), "true": sum(1 for r in strong if r["truth"])},
        "benchmark_negatives": {"n": len(negatives),
                                "stated_as_verified": sum(1 for r in negatives if r["after"] in VERIFIED),
                                "contradicted": sum(1 for r in negatives if r["contradicted"])},
        "checks_fired": {"on_false": dict(sorted(fired_false.items())), "on_true": dict(sorted(fired_true.items()))},
        "ms_per_claim": {"p50": ms[len(ms) // 2] if ms else None, "p95": ms[int(len(ms) * 0.95)] if ms else None},
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="python -m verinoda.benchmark.critique_eval")
    p.add_argument("--out")
    args = p.parse_args(argv)
    res = evaluate()
    from verinoda.benchmark.runner import environment

    res["environment"] = environment(None, None)
    if args.out:
        from verinoda.benchmark.sanitize import write_json

        # Written sanitised: no home, temp or checkout paths in a result meant to be committed.
        res = write_json(args.out, res)
    print(json.dumps(res, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    return 0 if res["benchmark_negatives"]["stated_as_verified"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
