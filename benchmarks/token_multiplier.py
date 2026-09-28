"""The token multiplier ("N x fewer tokens than reading every file"), with the facts each approach found.

The default and --check modes read committed result files only (standard library, no index, no network,
no tool run) and write benchmarks/results/token-multiplier-2026-09-28/multiplier.json; they print the
markdown tables of docs/BENCHMARKS.md, "Update 2026-09-28: the token multiplier, with its accuracy".
--measure-corpus runs git to write corpus.json next to it, the corpus sizes the other modes read.

    python benchmarks/token_multiplier.py            # write the JSON, print the tables
    python benchmarks/token_multiplier.py --check    # compare with the committed JSON, write nothing
    python benchmarks/token_multiplier.py --measure-corpus --graphify-repo PATH
        # measure the corpora from git objects and write corpus.json (needs git and an upstream Graphify
        # clone that has commit 20a20d30; the other corpora come from this repository)

Definitions:
- corpus tokens: ceil(chars / 4), chars = characters of the corpus's committed files read as UTF-8 with
  CRLF as one line break (the unit the answers are counted in: chars/4, and no answer carries a CR);
  measured by --measure-corpus into corpus.json;
- tokens per question: chars/4 of what the approach returned, mean over the set's questions;
- multiplier: corpus tokens / tokens per question; over several sets
  sum(corpus tokens x questions) / sum(answer tokens);
- facts per 1k tokens: facts found / answer tokens x 1000.

The script exits 1 if the eight sets' Graphify and raw-search rows do not add up to the totals of the
page's merged-night table, or if a corpus's file count differs from the one its run recorded.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "benchmarks" / "results"
OUT_DIR = RES / "token-multiplier-2026-09-28"
OUT = OUT_DIR / "multiplier.json"
CORPUS_OUT = OUT_DIR / "corpus.json"
VERINODA_FILE = "token-wins-2026-09-26/7-review-fixes.json"
BASELINE_TXT = "token-wins-2026-09-26/graphify-baseline-c8da753.txt"
QUESTIONS = ROOT / "verinoda" / "benchmark" / "questions"

# set -> (the result file its Graphify rows and raw-search row come from, or None for the rounded baseline
# table; the corpus it asks about)
SETS = {
    "orders_app": ("orders_app.json", "examples/orders_app"),
    "orders_app_tr": ("orders_app_tr.json", "examples/orders_app"),
    "glow_mod": ("mods-2026-09-24/final/glow_mod.json", "examples/glow_mod"),
    "forge_mod": ("mods-2026-09-24/final/forge_mod.json", "examples/forge_mod"),
    "graphify_core": ("graphify_core.json", "upstream Graphify 20a20d30"),
    "graphify_core_tr": ("graphify_core_tr.json", "upstream Graphify 20a20d30"),
    "heldout_repoatlas": ("heldout_repoatlas.json", "heldout_repoatlas_7371990"),
    "verinoda_user_tr": (None, "verinoda 3bd1b94"),
}
# the commit of this repository the unpinned corpora are measured at (main when this section was written; the
# runs copied a working tree, and their commits are not in the public history)
MAIN_COMMIT = "849ca6379b4b3dd47c6517c45f12499d841ffcd2"
# corpus -> where its committed files are: repository ("this" or "graphify"), commit, path prefix inside
# that commit, and the set whose record gives include/exclude
CORPORA = {
    "examples/orders_app": ("this", MAIN_COMMIT, "examples/orders_app/", "orders_app"),
    "examples/glow_mod": ("this", MAIN_COMMIT, "examples/glow_mod/", "glow_mod"),
    "examples/forge_mod": ("this", MAIN_COMMIT, "examples/forge_mod/", "forge_mod"),
    "upstream Graphify 20a20d30": ("graphify", "20a20d30d8e7eef77675651f0199d87f913bd3e7", "", "graphify_core"),
    "heldout_repoatlas_7371990": ("this", MAIN_COMMIT, "benchmarks/corpora/heldout_repoatlas_7371990/",
                                  "heldout_repoatlas"),
    "verinoda 3bd1b94": ("this", "3bd1b94a5cfcd8689d423bcb0dafa06974f8769f", "", "verinoda_user_tr"),
}
MERGED_NIGHT = {  # docs/BENCHMARKS.md, "Update 2026-09-26: the merged night": found, total, tokens/question
    "graphify_vendored": (69, 319, 1391),
    "graphify_cli": (68, 319, 1480),
    "raw": (129, 319, 3991),
}
APPROACHES = {  # key -> (label, where the row comes from)
    "verinoda_query": ("Verinoda query (text)", "q_text"),
    "verinoda_analyze": ("Verinoda analyze (text)", "an_bench"),
    "graphify_vendored": ("Graphify, vendored renderer", "graphify_vendored"),
    "graphify_cli": ("Graphify, upstream CLI", "graphify_cli"),
    "raw": ("raw text search", "raw"),
}


def _load(rel: str) -> dict:
    return json.loads((RES / rel).read_text(encoding="utf-8"))


def _selection(set_name: str) -> tuple[list[str] | None, list[str] | None]:
    """include / exclude of a set: its run's corpus record, else its question file's pinned corpus."""
    rel = SETS[set_name][0]
    rec = _load(rel)["corpus"] if rel else json.loads(
        (QUESTIONS / f"{set_name}.json").read_text(encoding="utf-8"))["corpus"]
    return rec.get("include"), rec.get("exclude")


def _selected(rel: str, include: list[str] | None, exclude: list[str] | None) -> bool:
    """The benchmark harness's selection (verinoda/benchmark/approaches.py, _selected)."""
    if include and not any(rel == i.rstrip("/") or rel.startswith(i.rstrip("/") + "/") for i in include):
        return False
    return not any(fnmatch(rel, pat) for pat in exclude or [])


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True,
                          check=True).stdout


def measure_corpus(repo: Path, commit: str, prefix: str, include, exclude) -> dict:
    """Files, bytes, CRLFs, chars, NUL and non-UTF-8 files of the committed blobs the selection keeps."""
    entries = []
    for rec in _git(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", commit, "--", prefix or ".").split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, kind, sha, _size = meta.split()
        rel = path.decode("utf-8")[len(prefix):]
        if kind == b"blob" and mode != b"120000" and _selected(rel, include, exclude):
            entries.append((rel, sha.decode()))
    data = _git(repo, "cat-file", "--batch", stdin="".join(f"{s}\n" for _r, s in entries).encode())
    out = {"files": len(entries), "bytes": 0, "crlf": 0, "crlf_files": 0, "line_ends": 0, "chars": 0,
           "nul_files": 0, "non_utf8_files": 0}
    pos = 0
    for _rel, sha in entries:
        head_end = data.index(b"\n", pos)
        got_sha, _kind, size = data[pos:head_end].split()
        assert got_sha.decode() == sha, (got_sha, sha)
        blob = data[head_end + 1:head_end + 1 + int(size)]
        pos = head_end + 1 + int(size) + 1
        out["bytes"] += len(blob)
        out["crlf"] += blob.count(b"\r\n")
        out["crlf_files"] += b"\r\n" in blob
        out["line_ends"] += blob.count(b"\n")
        out["nul_files"] += b"\0" in blob
        lf = blob.replace(b"\r\n", b"\n")
        try:
            text = lf.decode("utf-8")
        except UnicodeDecodeError:
            out["non_utf8_files"] += 1
            text = lf.decode("utf-8", errors="replace")
        out["chars"] += len(text)
    return out


def write_corpus(graphify_repo: Path) -> list[str]:
    errors: list[str] = []
    corpora = {}
    for cid, (where, sha, prefix, set_name) in CORPORA.items():
        repo = ROOT if where == "this" else graphify_repo
        include, exclude = _selection(set_name)
        m = measure_corpus(repo, sha, prefix, include, exclude)
        runs = {}
        for name, (rel, corpus) in SETS.items():
            if corpus == cid and rel:
                rec = _load(rel)["corpus"]
                runs[name] = {"file": rel, "file_count": rec["file_count"], "bytes": rec["bytes"],
                              "snapshot": rec["snapshot"]}
                if rec["file_count"] != m["files"]:
                    errors.append(f"{cid}: {m['files']} files, {rel} recorded {rec['file_count']}")
        corpora[cid] = {"repository": "this repository" if where == "this" else "upstream Graphify",
                        "commit": sha, "path": prefix or ".", "include": include, "exclude": exclude,
                        **m, "tokens": math.ceil(m["chars"] / 4), "runs_recorded": runs}
    out = {"generated_by": "python benchmarks/token_multiplier.py --measure-corpus --graphify-repo "
                           "<a clone of upstream Graphify with commit 20a20d30>",
           "method": "git ls-tree + git cat-file of the commit (the committed bytes, no checkout and no line-ending "
                     "conversion), the benchmark harness's include/exclude; chars = UTF-8 characters with CRLF "
                     "read as LF; tokens = ceil(chars / 4). runs_recorded: what each run recorded (its bytes are "
                     "the files as copied or exported at run time)",
           "corpora": corpora}
    CORPUS_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(CORPUS_OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    for cid, c in corpora.items():
        rec = "; ".join(f"{r['file']}: {r['file_count']} files, {r['bytes']:,} bytes" for r in c["runs_recorded"].values())
        print(f"{cid} at {c['commit'][:7]}: {c['files']} files, {c['bytes']:,} bytes, {c['crlf']:,} CRLF in {c['crlf_files']} files, "
              f"{c['line_ends']:,} line ends, {c['chars']:,} chars -> {c['tokens']:,} tokens, NUL files "
              f"{c['nul_files']}, non-UTF-8 files {c['non_utf8_files']}" + (f" (recorded: {rec})" if rec else ""))
    return errors


def baseline_rows() -> dict:
    """{(set, approach): (found, total, tokens per question)} from the rounded text table."""
    rows = {}
    pat = re.compile(r"^(\S+)\s+(\S+)\s+(\d+)/(\d+)\s+\d+\s+\d+\s+\d+\s+(\d+)\s")
    for line in (RES / BASELINE_TXT).read_text(encoding="utf-8").splitlines():
        m = pat.match(line)
        if m and m.group(1) != "all":
            rows[(m.group(1), m.group(2))] = (int(m.group(3)), int(m.group(4)), int(m.group(5)))
    return rows


def _row(found: int, total: int, answer_tokens: float, n_q: int, source: str, corpus_tokens: int) -> dict:
    tpq = answer_tokens / n_q
    return {"found": found, "total": total, "tokens_per_question": round(tpq, 1),
            "answer_tokens": round(answer_tokens), "multiplier": round(corpus_tokens / tpq, 2),
            "facts_per_1k_tokens": round(found / answer_tokens * 1000, 2), "source": source}


def compute() -> tuple[dict, list[str]]:
    errors: list[str] = []
    base = baseline_rows()
    ver = _load(VERINODA_FILE)
    corpus_file = json.loads(CORPUS_OUT.read_text(encoding="utf-8"))["corpora"]
    sets = {}
    for name, (rel, cid) in SETS.items():
        c = corpus_file[cid]
        ctoks = c["tokens"]
        n_q = ver[name]["q_text"]["n_q"]
        n_file = len(json.loads((QUESTIONS / f"{name}.json").read_text(encoding="utf-8"))["questions"])
        if n_file != n_q:
            errors.append(f"{name}: {VERINODA_FILE} has {n_q} questions, the question file {n_file}")
        res = _load(rel) if rel else None
        if res is not None and res["corpus"]["file_count"] != c["files"]:
            errors.append(f"{name}: corpus.json has {c['files']} files, {rel} {res['corpus']['file_count']}")
        summary = res["summary"] if res else {}
        rows = {}
        for key, (_label, src) in APPROACHES.items():
            if key.startswith("verinoda"):
                v = ver[name][src]
                rows[key] = _row(v["found"], v["total"], v["tokens"], v["n_q"], f"{VERINODA_FILE} {src}", ctoks)
            elif src in summary:
                s = summary[src]
                if s["questions"] != n_q:
                    errors.append(f"{name} {src}: {rel} has {s['questions']} questions, {VERINODA_FILE} {n_q}")
                rows[key] = _row(s["facts_found"], s["facts_total"], s["tokens_total"], s["questions"],
                                 f"{rel} summary.{src}", ctoks)
                if abs(rows[key]["facts_per_1k_tokens"] - s["facts_per_1k_tokens"]) > 0.01:
                    errors.append(f"{name} {src}: facts per 1k {rows[key]['facts_per_1k_tokens']}, "
                                  f"{rel} {s['facts_per_1k_tokens']}")
                b = base.get((name, src))
                if b and (b[0] != s["facts_found"] or abs(b[2] - s["tokens_mean"]) > 0.5):
                    errors.append(f"{name} {src}: {rel} gives {s['facts_found']} at {s['tokens_mean']}, "
                                  f"{BASELINE_TXT} {b[0]} at {b[2]}")
            else:  # not in the set's run (the CLI on the mod sets; every row of verinoda_user_tr): rounded table
                found, total, tpq = base[(name, src)]
                rows[key] = _row(found, total, tpq * n_q, n_q, f"{BASELINE_TXT} (rounded tokens/question)", ctoks)
        totals = {r["total"] for r in rows.values()}
        if len(totals) != 1:
            errors.append(f"{name}: gold totals differ between sources: {sorted(totals)}")
        run = None
        if res is not None:
            env = res["environment"]
            run = {"file": rel, "date_utc": env["date_utc"],
                   "graphify_cli": (env.get("graphify_cli") or {}).get("version")}
        sets[name] = {"corpus": {"id": cid, "files": c["files"], "chars": c["chars"], "tokens": ctoks,
                                 "source": f"corpus.json {cid}"},
                      "questions": n_q, "graphify_raw_run": run, "approaches": rows}

    totals = {}
    nq = sum(s["questions"] for s in sets.values())
    corpus_q = sum(s["corpus"]["tokens"] * s["questions"] for s in sets.values())
    for key, (label, _src) in APPROACHES.items():
        found = sum(s["approaches"][key]["found"] for s in sets.values())
        total = sum(s["approaches"][key]["total"] for s in sets.values())
        ans = sum(s["approaches"][key]["answer_tokens"] for s in sets.values())
        mults = [s["approaches"][key]["multiplier"] for s in sets.values()]
        totals[key] = {"label": label, "found": found, "total": total, "tokens_per_question": round(ans / nq, 1),
                       "multiplier": round(corpus_q / ans, 1), "multiplier_min": min(mults),
                       "multiplier_max": max(mults), "facts_per_1k_tokens": round(found / ans * 1000, 2)}
    distinct = {s["corpus"]["id"]: s["corpus"]["files"] for s in sets.values()}

    # the merged-night check: the eight sets' Graphify and raw rows add up to the page's merged-night table
    check = {}
    for src, (want_found, want_total, want_tpq) in MERGED_NIGHT.items():
        t = totals[src]
        ok = (t["found"], t["total"]) == (want_found, want_total) and abs(t["tokens_per_question"] - want_tpq) <= 1
        check[src] = {"found": t["found"], "total": t["total"], "tokens_per_question": t["tokens_per_question"],
                      "merged_night": f"{want_found}/{want_total} at {want_tpq}", "ok": ok}
        if not ok:
            errors.append(f"merged-night check {src}: {t['found']}/{t['total']} at {t['tokens_per_question']} "
                          f"!= {want_found}/{want_total} at {want_tpq}")

    out = {
        "generated_by": "python benchmarks/token_multiplier.py",
        "method": {
            "corpus_tokens": "ceil(chars / 4); chars = UTF-8 characters of the corpus's committed files with CRLF "
                             "read as LF, the files the set's include/exclude selects (corpus.json)",
            "tokens_per_question": "chars/4 of what the approach returned, mean over the set's questions",
            "multiplier": "corpus tokens / tokens per question; all sets: sum(corpus tokens x questions) / "
                          "sum(answer tokens)",
            "facts_per_1k_tokens": "facts found / answer tokens x 1000",
            "baseline": "reading every file of the corpus for every question (the baseline of Graphify's and "
                        "code-review-graph's headline numbers)",
        },
        "sources": {"root": "benchmarks/results", "corpus": "token-multiplier-2026-09-28/corpus.json",
                    "verinoda": VERINODA_FILE,
                    "graphify_and_raw": {k: v[0] for k, v in SETS.items() if v[0]},
                    "rounded_table": BASELINE_TXT,
                    "rounded_table_rows": "graphify_cli on glow_mod and forge_mod; every Graphify and raw row "
                                          "of verinoda_user_tr"},
        "sets": sets,
        "totals": {"sets": list(sets), "questions": nq, "corpus_tokens_per_question": round(corpus_q / nq, 1),
                   "distinct_corpora": len(distinct), "distinct_files": sum(distinct.values()),
                   "approaches": totals},
        "merged_night_check": check,
    }
    return out, errors


_WORDS = {7: "seven", 8: "eight"}


def _mult(x: float) -> str:
    return f"{x:,.1f}x" if x < 10 else f"{x:,.0f}x"


def markdown(out: dict) -> str:
    keys = list(APPROACHES)
    head = ["set", "files", "corpus tokens"] + [APPROACHES[k][0] for k in keys]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for name, s in out["sets"].items():
        cells = [f"`{name}`", str(s["corpus"]["files"]), f"{s['corpus']['tokens']:,}"]
        for k in keys:
            r = s["approaches"][k]
            cells.append(f"{_mult(r['multiplier'])} · {r['tokens_per_question']:,.0f} · {r['found']}/{r['total']}")
        lines.append("| " + " | ".join(cells) + " |")
    t = out["totals"]
    n = _WORDS.get(len(out["sets"]), str(len(out["sets"])))
    cells = [f"**all {n}**", f"{t['distinct_files']} distinct",
             f"{t['corpus_tokens_per_question']:,.0f} per question"]
    for k in keys:
        r = t["approaches"][k]
        cells.append(f"**{_mult(r['multiplier'])} · {r['tokens_per_question']:,.0f} · {r['found']}/{r['total']}**")
    lines.append("| " + " | ".join(cells) + " |")
    lines += ["", f"| approach | facts found | tokens per question | facts per 1k tokens | multiplier, all {n} "
              f"| multiplier per set |", "|---|---|---|---|---|---|"]
    for k in keys:
        r = t["approaches"][k]
        lines.append(f"| {r['label']} | {r['found']}/{r['total']} | {r['tokens_per_question']:,.0f} | "
                     f"{r['facts_per_1k_tokens']:.2f} | {_mult(r['multiplier'])} | "
                     f"{_mult(r['multiplier_min'])} - {_mult(r['multiplier_max'])} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="compare with the committed JSON, write nothing")
    ap.add_argument("--measure-corpus", action="store_true",
                    help="measure the corpora from git objects and write corpus.json (needs --graphify-repo)")
    ap.add_argument("--graphify-repo", type=Path, help="a clone of upstream Graphify that has commit 20a20d30")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the tables use a middle dot
    if args.measure_corpus:
        if args.graphify_repo is None:
            ap.error("--measure-corpus needs --graphify-repo")
        errors = write_corpus(args.graphify_repo)
        for e in errors:
            print("error:", e, file=sys.stderr)
        return 1 if errors else 0
    out, errors = compute()
    text = json.dumps(out, indent=1, ensure_ascii=False) + "\n"
    if args.check:
        same = OUT.is_file() and OUT.read_text(encoding="utf-8") == text
        print("multiplier.json is up to date" if same else "multiplier.json differs from a fresh computation")
        errors += [] if same else ["stale multiplier.json"]
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(markdown(out))
        print()
        for src, c in out["merged_night_check"].items():
            print(f"merged-night check {src}: {c['found']}/{c['total']} at {c['tokens_per_question']:,} "
                  f"(page: {c['merged_night']}) {'ok' if c['ok'] else 'MISMATCH'}")
    for e in errors:
        print("error:", e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
