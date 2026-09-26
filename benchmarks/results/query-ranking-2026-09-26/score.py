"""Score `verinoda query` ranking on a question file: where the gold file ranks, and how many tests fill the top.

usage:
  python score.py QUESTIONS.json --code DIR [--corpus NAME=PATH ...] [--out RESULT.json] [--cli] [--only ID,ID]
  python score.py --compare A.json B.json          (two result files of this script, per question)

--code DIR   a directory that holds the `verinoda` package to measure (the base copy, or a worktree root).
             The scorer puts DIR first on sys.path, drops editable-install finders and checks that
             verinoda.__file__ is inside DIR. Run it with the repository's venv python.
--corpus     where a question's "corpus" name points (repeatable). Built in: vn, gfy, big, orders, glow,
             forge -> the indexed copies in corp/ next to this file (elsewhere, pass --corpus NAME=PATH for
             each). A corpus value that is an existing
             directory is used as it is. Every corpus must already be indexed (`verinoda scan`); the
             scorer never indexes, so it never writes into a source folder. Ranking-only changes need no
             re-index.
--cli        run `python -m verinoda query --json` per question (the real command) instead of the
             in-process call retrieval.retrieve(g, q, Budget(max_items=10, max_chars=6000)), which is what
             cmd_query does. Both give the same list; in-process is faster (one graph load per corpus).

Question file: a JSON list, or {"questions": [...]}. Each question:
  {"id": "...", "corpus": "big", "question": "...",
   "gold_files": ["subprocess.py"], "gold_symbols": ["list2cmdline"], "asks_tests": false}
  ("gold": {"files": [...], "symbols": [...]} is accepted too; a gold file matches a ranked file when
  the two are equal or the ranked path ends with "/" + gold.)

Ranked list = the JSON items in order, then the files of budget.more (the further candidates the text
shows as "path:a-b name"). Metrics, over the questions:
  top1/top3/top10: share whose first gold-file hit is at rank <= k;   MRR: mean of 1/rank (0 if absent);
  sym_top3: the same for a gold symbol (item symbol or `more` name ends with it) when gold_symbols is set;
  tests_top5: when asks_tests is false, test files among the first 5 ranked entries (sum over questions),
  test_first: questions whose first entry is a test.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPORA = {n: HERE / "corp" / n for n in ("vn", "gfy", "big", "orders", "glow", "forge")}
PY = sys.executable

# the scorer's own test-file rule (independent of the code under test)
TEST_RX = re.compile(
    r"(^|/)(tests?|testing|__tests__|spec|specs|idle_test|test_support|gametest|testdata|fixtures)/"
    r"|(^|/)test_[^/]+\.py$|(^|/)[^/]+_test\.(py|go)$|(^|/)conftest\.py$|\.(test|spec)\.[jt]sx?$"
    r"|(^|/)src/(test|gametest|integrationTest|testFixtures)[^/]*/"
    r"|(Test|Tests|IT)\.(java|kt|cs)$", re.IGNORECASE)


def is_test(path: str) -> bool:
    return bool(TEST_RX.search(path))


def load_questions(p: Path) -> list[dict]:
    d = json.loads(p.read_text(encoding="utf-8"))
    qs = d["questions"] if isinstance(d, dict) else d
    out = []
    for q in qs:
        gold = q.get("gold") or {}
        if isinstance(gold, list):
            gold = {"files": gold}
        files = q.get("gold_files") or gold.get("files") or []
        syms = q.get("gold_symbols") or gold.get("symbols") or []
        out.append({**q, "gold_files": [f.replace("\\", "/").strip("/") for f in files],
                    "gold_symbols": list(syms), "asks_tests": bool(q.get("asks_tests"))})
    return out


def file_hit(ranked: str, gold: list[str]) -> bool:
    r = ranked.replace("\\", "/")
    return any(r == g or r.endswith("/" + g) for g in gold)


def sym_hit(name: str, gold: list[str]) -> bool:
    n = (name or "").strip()
    n = re.sub(r"\(.*$", "", n).strip(". ")
    return any(n == g or n.endswith("." + g) or n.split(".")[-1] == g for g in gold)


def ranked_of(res: dict) -> list[tuple[str, str]]:
    """(file, symbol) in the order the result shows them."""
    out = [(i["file"], i.get("symbol") or "") for i in res.get("items", [])]
    for e in (res.get("budget") or {}).get("more", []):
        m = re.match(r"^(.+?):(\d+)-(\d+) (.*)$", e)
        if m:
            out.append((m.group(1), m.group(4)))
    return out


def run_inprocess(code: Path, jobs: list[tuple[dict, Path]]) -> dict[str, dict]:
    sys.meta_path[:] = [f for f in sys.meta_path if "editable" not in type(f).__module__.lower()
                        and "editable" not in getattr(f, "__name__", "").lower()]
    sys.path.insert(0, str(code))
    import verinoda

    assert str(Path(verinoda.__file__).resolve()).startswith(str(code.resolve())), verinoda.__file__
    from verinoda import index, retrieval

    if os.environ.get("QR_SET"):  # experiment: QR_SET="NAME=value,..." sets search_index constants
        from verinoda import search_index as si

        for kv in os.environ["QR_SET"].split(","):
            k, v = kv.split("=")
            setattr(si, k, float(v))

    out: dict[str, dict] = {}
    graphs: dict[Path, object] = {}
    for q, repo in jobs:
        if repo not in graphs:
            graphs[repo] = index.load(repo)
        t0 = time.perf_counter()
        res = retrieval.retrieve(graphs[repo], q["question"], retrieval.Budget(max_items=10, max_chars=6000))
        out[q["id"]] = {"items": res["items"], "budget": res["budget"], "seconds": time.perf_counter() - t0}
    return out


def run_cli(code: Path, jobs: list[tuple[dict, Path]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    boot = ("import sys,runpy;sys.meta_path[:]=[f for f in sys.meta_path if 'editable' not in "
            "type(f).__module__.lower() and 'editable' not in getattr(f,'__name__','').lower()];"
            f"sys.path.insert(0,{str(code)!r});sys.argv=['verinoda']+sys.argv[1:];"
            "runpy.run_module('verinoda',run_name='__main__',alter_sys=True)")
    for q, repo in jobs:
        t0 = time.perf_counter()
        r = subprocess.run([PY, "-c", boot, "query", q["question"], "--json", "--repo", str(repo)],
                           capture_output=True, text=True, encoding="utf-8", cwd=str(code), check=False)
        if r.returncode != 0:
            raise SystemExit(f"{q['id']}: query failed: {r.stderr[-800:]}")
        res = json.loads(r.stdout)
        out[q["id"]] = {"items": res["items"], "budget": res["budget"], "seconds": time.perf_counter() - t0}
    return out


def score(qs: list[dict], raw: dict[str, dict]) -> dict:
    rows = []
    for q in qs:
        res = raw[q["id"]]
        rk = ranked_of(res)
        frank = next((k + 1 for k, (f, _s) in enumerate(rk) if file_hit(f, q["gold_files"])), None)
        srank = next((k + 1 for k, (f, s) in enumerate(rk) if sym_hit(s, q["gold_symbols"])
                      and (not q["gold_files"] or file_hit(f, q["gold_files"]))), None) if q["gold_symbols"] else None
        top5 = [f for f, _s in rk[:5]]
        rows.append({"id": q["id"], "corpus": q.get("corpus"), "asks_tests": q["asks_tests"], "file_rank": frank,
                     "sym_rank": srank, "has_sym": bool(q["gold_symbols"]), "tests_top5": sum(map(is_test, top5)),
                     "test_first": bool(rk) and is_test(rk[0][0]), "n_ranked": len(rk),
                     "top": [f"{f} {s}"[:110] for f, s in rk[:5]], "seconds": round(res.get("seconds", 0.0), 2)})
    n = len(rows) or 1
    nt = [r for r in rows if not r["asks_tests"]]
    ws = [r for r in rows if r["has_sym"]]
    summ = {
        "questions": len(rows),
        "top1": sum(1 for r in rows if r["file_rank"] and r["file_rank"] <= 1),
        "top3": sum(1 for r in rows if r["file_rank"] and r["file_rank"] <= 3),
        "top10": sum(1 for r in rows if r["file_rank"] and r["file_rank"] <= 10),
        "MRR": round(sum(1 / r["file_rank"] for r in rows if r["file_rank"]) / n, 3),
        "sym_top3": f"{sum(1 for r in ws if r['sym_rank'] and r['sym_rank'] <= 3)}/{len(ws)}",
        "no_test_questions": len(nt),
        "tests_top5": f"{sum(r['tests_top5'] for r in nt)}/{5 * len(nt)}",
        "test_first": sum(1 for r in nt if r["test_first"]),
    }
    return {"summary": summ, "rows": rows}


def print_result(res: dict, verbose: bool) -> None:
    for r in res["rows"]:
        flag = "T" if r["asks_tests"] else " "
        print(f"{r['id']:<16}{flag} file@{r['file_rank']!s:<4} sym@{r['sym_rank']!s:<4} tests5={r['tests_top5']}"
              f"{' TEST#1' if r['test_first'] and not r['asks_tests'] else ''}")
        if verbose:
            for t in r["top"]:
                print("      ", t)
    s = res["summary"]
    print(f"\n{s['questions']} questions: top1 {s['top1']}  top3 {s['top3']}  top10 {s['top10']}  MRR {s['MRR']}  "
          f"sym_top3 {s['sym_top3']}  | no-test questions {s['no_test_questions']}: tests in top5 "
          f"{s['tests_top5']}, test first {s['test_first']}")


def compare(a: dict, b: dict) -> None:
    ra = {r["id"]: r for r in a["rows"]}
    for r in b["rows"]:
        x = ra.get(r["id"])
        if x and (x["file_rank"], x["tests_top5"], x["sym_rank"]) != (r["file_rank"], r["tests_top5"], r["sym_rank"]):
            print(f"{r['id']:<16} file {x['file_rank']} -> {r['file_rank']}   sym {x['sym_rank']} -> {r['sym_rank']}"
                  f"   tests5 {x['tests_top5']} -> {r['tests_top5']}")
    for k in ("top1", "top3", "top10", "MRR", "sym_top3", "tests_top5", "test_first"):
        print(f"  {k:<10} {a['summary'][k]} -> {b['summary'][k]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("questions", nargs="?")
    ap.add_argument("--code")
    ap.add_argument("--corpus", action="append", default=[])
    ap.add_argument("--out")
    ap.add_argument("--cli", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--compare", nargs=2)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.compare:
        x, y = (json.loads(Path(p).read_text(encoding="utf-8")) for p in a.compare)
        compare(x, y)
        return
    if not a.questions or not a.code:
        ap.error("QUESTIONS and --code are required")
    corpora = dict(CORPORA)
    for kv in a.corpus:
        k, v = kv.split("=", 1)
        corpora[k] = Path(v)
    qs = load_questions(Path(a.questions))
    if a.only:
        keep = set(a.only.split(","))
        qs = [q for q in qs if q["id"] in keep]
    jobs = []
    for q in qs:
        c = q.get("corpus")
        repo = corpora.get(c) or (Path(c) if c and Path(c).is_dir() else None)
        if repo is None:
            raise SystemExit(f"{q['id']}: unknown corpus {c!r}; pass --corpus {c}=PATH")
        if not (repo / ".verinoda" / "index").exists():
            raise SystemExit(f"{q['id']}: {repo} has no index; index a copy of it (verinoda scan) and pass that")
        jobs.append((q, repo.resolve()))
    code = Path(a.code).resolve()
    raw = run_cli(code, jobs) if a.cli else run_inprocess(code, jobs)
    res = score(qs, raw)
    res["code"] = str(code)
    res["questions_file"] = str(Path(a.questions).resolve())
    print_result(res, a.verbose)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
