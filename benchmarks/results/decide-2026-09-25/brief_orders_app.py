"""decisions_v1 (b): the decision brief on a git copy of orders_app, EN and TR question.

Gold (from the design, written before the brief code): 8 forces, 5 human questions. Evidence precision
is checked independently of the brief's own re-check: every cited file line is re-read and must equal
the excerpt the brief recorded (git evidence: the commit must exist). A question "answered by the code"
is one whose answer a probe found (the gold list below): none may be asked.
usage: python benchmarks/results/decide-2026-09-25/brief_orders_app.py [question ...]  (writes brief_orders_app.json)
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
V = HERE.parents[2]
sys.path.insert(0, str(V))
WORK = Path(tempfile.mkdtemp(prefix="decide-bench-"))

from verinoda import decision_brief as dbr  # noqa: E402
from verinoda import index  # noqa: E402
from verinoda.store import open_store  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def rmtree(p: Path) -> None:
    def onerr(fn, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        fn(path)

    shutil.rmtree(p, onerror=onerr)


def make(name: str, src: Path) -> Path:
    """A fresh git copy of an example under the work folder, scanned."""
    from verinoda import workflow
    from verinoda.store import open_store

    dst = WORK / name
    if dst.exists():
        rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache",
                                                            "*.db", ".git"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst

QUESTIONS = ["Should we move orders from SQLite to PostgreSQL if traffic grows?",
             "Sipariş sayısı artarsa SQLite'tan PostgreSQL'e geçmeli miyiz?"]
ADR = "docs/adr/0001-sqlite-persistence.md"


def locs(f):
    return [e["locator"] for e in f.get("evidence") or []]


GOLD_FORCES = {
    "1 sqlite3.connect at orders/repository.py:10":
        lambda b: any("sqlite3.connect" in f["fact"] and "orders/repository.py:10" in locs(f) for f in b["forces"]),
    "2 ORDERS_DATABASE_URL default orders.db at orders/config.py:5":
        lambda b: any("ORDERS_DATABASE_URL" in f["fact"] and "orders.db" in f["fact"] and "orders/config.py:5" in
                      locs(f) for f in b["forces"]),
    "3 ADR-0001 accepted, same repository interface, at the ADR:5-7":
        lambda b: any(f"{ADR}:5-7" in locs(f) and "accepted" in f["fact"] and "same repository interface" in f["fact"]
                      for f in b["forces"]),
    "4 all storage sinks in one file":
        lambda b: any(re.search(r"all \d+ storage sink.* in one file: orders/repository\.py", f["fact"])
                      for f in b["forces"]),
    "5 module-level shared repository at orders/api.py:6-13":
        lambda b: any("_repo" in f["fact"] and locs(f) and all(re.match(r"orders/api\.py:(\d+)(?:-(\d+))?$", x) and
                      6 <= int(re.match(r".*:(\d+)", x).group(1)) <= 13 for x in locs(f)) for f in b["forces"]),
    "6 no deployment files (absence with globs)":
        lambda b: any("deployment" in a["what"] and "Dockerfile" in a["searched"] and
                      any("compose" in s for s in a["searched"]) for a in b["absences"]),
    "7 no DB driver declared in pyproject.toml":
        lambda b: any("driver" in a["what"] and "pyproject.toml" in (a["what"] + a["scope_note"])
                      for a in b["absences"]),
    "8 tests pin :memory: at tests/test_service.py:8,15":
        lambda b: any(":memory:" in f["fact"] and {"tests/test_service.py:8", "tests/test_service.py:15"} <=
                      set(locs(f)) for f in b["forces"]),
}
GOLD_QUESTION_KINDS = ["concurrency", "volume", "hosting", "operations", "adr_reason"]
# what the code answers (a probe found it): asking any of these is a failure
ANSWERED_BY_CODE = [r"which (database|db|storage)\b.*\b(use|using)\b", r"what database", r"where .*(stored|saved)",
                    r"which python", r"is there a (dockerfile|deployment)", r"hangi veritaban\w* kullan",
                    r"nerede (saklan|kaydedil)"]


def evidence_ok(repo: Path, e: dict) -> bool:
    if e.get("source_type") == "git_history":
        sha = e.get("excerpt") or ""
        r = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True)
        return r.returncode == 0
    m = re.match(r"^(.+?):(\d+)(?:-(\d+))?$", e["locator"])
    if not m:
        return False
    lines = (repo / m.group(1)).read_text(encoding="utf-8").split("\n")
    a = int(m.group(2))
    return 0 < a <= len(lines) and lines[a - 1].strip()[:160] == e["excerpt"]


def main() -> None:
    repo = make("brief_orders", V / "examples/orders_app")
    qs = sys.argv[1:] or QUESTIONS
    out = {}
    for q in qs:
        st = open_store(repo)
        try:
            t0 = time.perf_counter()
            b = dbr.brief(repo, q, store=st, graph=index.load(repo))
            dt = time.perf_counter() - t0
        finally:
            st.close()
        forces = {k: bool(fn(b)) for k, fn in GOLD_FORCES.items()}
        evs = [e for f in b["forces"] for e in f["evidence"]] + \
            [e for o in b["options"] for e in o.get("presence_evidence") or []]
        ok = [evidence_ok(repo, e) for e in evs]
        asked = b["questions_for_human"]
        texts = [q_["text_en"].lower() + " | " + q_["text_tr"].lower() for q_ in asked]
        bad_q = [t for t in texts if any(re.search(rx, t) for rx in ANSWERED_BY_CODE)]
        kinds = [q_["kind"] for q_ in asked]
        out[q] = {
            "verdict": b["verdict"], "has_recommendation_field": any("recommend" in k for k in b),
            "gold_forces_found": sum(forces.values()), "gold_forces": forces,
            "evidence_items": len(evs), "evidence_rechecked": sum(ok),
            "evidence_precision": round(sum(ok) / len(ok), 3) if ok else None,
            "questions": len(asked), "question_kinds": kinds,
            "gold_questions_asked": sum(k in kinds for k in GOLD_QUESTION_KINDS),
            "questions_answered_by_code": bad_q, "answered_by_code_not_asked": b.get("answered_by_code"),
            "forces": len(b["forces"]), "absences": len(b["absences"]), "seconds": round(dt, 3),
            "options": [(o["name"], o["present_in_project"]) for o in b["options"]]}
        print(json.dumps({q: out[q]}, indent=1, ensure_ascii=False))
    (HERE / "brief_orders_app.json").write_bytes((json.dumps(out, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
