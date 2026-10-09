#!/usr/bin/env python3
"""Blind benchmark runner for the biology platform variants (stdlib only).

Usage:
    python run_benchmark.py <variant-dir> [--out <path>]

For each query in gold/queries.json it runs `python query.py "<q>"` inside the variant folder and scores the JSON it
prints against gold/relations.json and corpus/topics.json. The variant folder is never written to.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BENCH_DIR)
GOLD_DIR = os.path.join(BENCH_DIR, "gold")
TOPICS_PATH = os.path.join(ROOT_DIR, "corpus", "topics.json")
TIMEOUT_S = 60


# ---------------------------------------------------------------- helpers

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def turkish_lower(s):
    # str.lower() maps "I" to "i" and "İ" to "i̇"; Turkish wants "I"->"ı" and "İ"->"i".
    return s.replace("İ", "i").replace("I", "ı").lower()


def norm_text(s):
    s = unicodedata.normalize("NFC", s or "")
    # Typographic quotes/dashes are not the point of the check.
    s = (s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
         .replace("–", "-").replace("—", "-").replace(" ", " "))
    s = re.sub(r"\s+", " ", s).strip()
    return turkish_lower(s)


def pair(a, b):
    return frozenset((a, b))


def other_end(p, center):
    rest = [x for x in p if x != center]
    return rest[0] if rest else center


def ratio(num, den):
    return round(num / den, 4) if den else None


def is_nonempty_str(x):
    return isinstance(x, str) and x.strip() != ""


def is_nonempty_list(x):
    return isinstance(x, list) and len(x) > 0


# ---------------------------------------------------------------- running a variant

def run_query(variant_dir, q):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    started = time.perf_counter()
    rec = {"q": q, "ok": False, "error": None, "seconds": None, "output": None}
    try:
        proc = subprocess.run([sys.executable, "query.py", q], cwd=variant_dir, capture_output=True,
                              timeout=TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        rec["seconds"] = round(time.perf_counter() - started, 3)
        rec["error"] = "timeout after %ds" % TIMEOUT_S
        return rec
    except OSError as e:
        rec["seconds"] = round(time.perf_counter() - started, 3)
        rec["error"] = "could not start: %s" % e
        return rec
    rec["seconds"] = round(time.perf_counter() - started, 3)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        rec["error"] = "exit code %d: %s" % (proc.returncode, stderr.strip()[-500:])
        return rec
    try:
        out = json.loads(stdout.strip())
    except ValueError as e:
        rec["error"] = "bad JSON on stdout: %s (first 200 chars: %r)" % (e, stdout[:200])
        return rec
    if not isinstance(out, dict):
        rec["error"] = "stdout JSON is not an object"
        return rec
    rec["ok"] = True
    rec["output"] = out
    return rec


# ---------------------------------------------------------------- scoring

class PassageCache:
    def __init__(self, variant_dir):
        self.variant_dir = os.path.abspath(variant_dir)
        self.cache = {}

    def resolve(self, rel):
        """Resolve relative to the variant dir; fall back to the project root, where the shared corpus lives.

        A path that escapes its base (absolute, or through "..") is refused.
        """
        if not is_nonempty_str(rel):
            return None, None
        for base, via in ((self.variant_dir, "variant"), (ROOT_DIR, "root")):
            p = os.path.abspath(os.path.join(base, rel))
            try:
                if os.path.commonpath([p, base]) != base:
                    continue
            except ValueError:  # different drives on Windows
                continue
            if os.path.isfile(p):
                return p, via
        return None, None

    def text(self, rel):
        if rel not in self.cache:
            p, via = self.resolve(rel)
            if p is None:
                self.cache[rel] = (None, None)
            else:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    self.cache[rel] = (norm_text(f.read()), via)
        return self.cache[rel]


def score_query(query, rec, gold_rel, topics, passages):
    center = query["center"]
    gold = gold_rel.get(center, {"edges": [], "irrelevant": []})
    gold_edges = gold["edges"]
    gold_pairs = {pair(e["a"], e["b"]): e for e in gold_edges}
    irrelevant = set(gold.get("irrelevant", []))
    n_gold = len(gold_pairs)
    n_core = sum(1 for e in gold_edges if e.get("core"))
    n_onkosul = sum(1 for e in gold_edges if e["type"] == "onkosul")

    res = {
        "q": query["q"], "center": center, "ok": rec["ok"], "error": rec["error"], "seconds": rec["seconds"],
        "gold_edges": n_gold, "gold_core": n_core, "gold_onkosul": n_onkosul,
    }
    zero = {"recall_hits": 0, "typed_hits": 0, "core_hits": 0, "direction_hits": 0,
            "out_center_edges": 0, "correct": 0, "wrong": 0, "unjudged": 0,
            "out_edges": 0, "verified": 0, "verified_ok": 0, "fake_verified": 0, "no_evidence": 0,
            "nodes": 0, "panel_complete": 0, "panel_summary": 0, "panel_uses": 0, "panel_sources": 0,
            "grades_ok": 0}
    res.update(zero)
    if not rec["ok"]:
        return res

    out = rec["output"]
    res["output_center"] = out.get("center")
    res["center_match"] = out.get("center") == center
    edges = out.get("edges") or []
    nodes = out.get("nodes") or []
    if not isinstance(edges, list):
        edges = []
    if not isinstance(nodes, list):
        nodes = []

    # Edges touching the gold centre, grouped by unordered pair.
    by_pair = {}
    for e in edges:
        if not isinstance(e, dict):
            continue
        s, t = e.get("source"), e.get("target")
        if center not in (s, t) or s == t:
            continue
        by_pair.setdefault(pair(s, t), []).append(e)

    found, missed, typed_wrong, dir_wrong = [], [], [], []
    for p, ge in gold_pairs.items():
        out_es = by_pair.get(p, [])
        label = "%s-%s" % (ge["a"], ge["b"])
        if not out_es:
            missed.append(label)
            continue
        res["recall_hits"] += 1
        found.append(label)
        if ge.get("core"):
            res["core_hits"] += 1
        if any(x.get("type") == ge["type"] for x in out_es):
            res["typed_hits"] += 1
        else:
            typed_wrong.append("%s (gold %s, got %s)" % (label, ge["type"],
                                                          ",".join(sorted({str(x.get("type")) for x in out_es}))))
        if ge["type"] == "onkosul":
            if any(x.get("type") == "onkosul" and x.get("source") == ge["from"] and x.get("target") == ge["to"]
                   for x in out_es):
                res["direction_hits"] += 1
            else:
                dir_wrong.append("%s->%s" % (ge["from"], ge["to"]))
    res["found"], res["missed"], res["typed_wrong"], res["direction_wrong"] = found, missed, typed_wrong, dir_wrong

    wrong_ids, unjudged_ids = [], []
    for p in by_pair:
        res["out_center_edges"] += 1
        if p in gold_pairs:
            res["correct"] += 1
        elif other_end(p, center) in irrelevant:
            res["wrong"] += 1
            wrong_ids.append(other_end(p, center))
        else:
            res["unjudged"] += 1
            unjudged_ids.append(other_end(p, center))
    res["wrong_ids"], res["unjudged_ids"] = sorted(wrong_ids), sorted(unjudged_ids)

    # Evidence over every output edge, not only the ones touching the centre.
    fake = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        res["out_edges"] += 1
        ev = e.get("evidence")
        ev = [x for x in ev if isinstance(x, dict)] if isinstance(ev, list) else []
        if not ev:
            res["no_evidence"] += 1
        if e.get("status") != "verified":
            continue
        res["verified"] += 1
        problems = []
        if not ev:
            problems.append("verified with no evidence")
        for x in ev:
            body, _via = passages.text(x.get("passage"))
            quote = norm_text(x.get("quote") or "")
            if body is None:
                problems.append("passage not found: %r" % x.get("passage"))
            elif not quote:
                problems.append("empty quote")
            elif quote not in body:
                problems.append("quote not in %s: %r" % (x.get("passage"), (x.get("quote") or "")[:80]))
        if problems:
            res["fake_verified"] += 1
            fake.append({"edge": "%s->%s" % (e.get("source"), e.get("target")), "problems": problems})
        else:
            res["verified_ok"] += 1
    res["fake_verified_detail"] = fake

    grade_bad = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        res["nodes"] += 1
        study = n.get("study") if isinstance(n.get("study"), dict) else {}
        s_ok = is_nonempty_str(n.get("summary"))
        u_ok = is_nonempty_list(n.get("uses"))
        src_ok = is_nonempty_list(study.get("sources"))
        res["panel_summary"] += s_ok
        res["panel_uses"] += u_ok
        res["panel_sources"] += src_ok
        res["panel_complete"] += s_ok and u_ok and src_ok
        nid = n.get("id")
        want = topics.get(nid)
        got = n.get("grades")
        if want is not None and isinstance(got, list) and sorted(got) == sorted(want):
            res["grades_ok"] += 1
        else:
            grade_bad.append({"id": nid, "got": got, "want": want})
    res["grades_wrong"] = grade_bad
    return res


def aggregate(per_query):
    tot = {}
    keys = ["gold_edges", "gold_core", "gold_onkosul", "recall_hits", "typed_hits", "core_hits", "direction_hits",
            "out_center_edges", "correct", "wrong", "unjudged", "out_edges", "verified", "verified_ok",
            "fake_verified", "no_evidence", "nodes", "panel_complete", "panel_summary", "panel_uses",
            "panel_sources", "grades_ok"]
    for k in keys:
        tot[k] = sum(r[k] for r in per_query)
    times = [r["seconds"] for r in per_query if r["seconds"] is not None]
    agg = {
        "queries": len(per_query),
        "failures": sum(1 for r in per_query if not r["ok"]),
        "center_mismatches": sum(1 for r in per_query if r["ok"] and not r.get("center_match")),
        # Micro-averaged: a failed query keeps its gold edges in the denominator.
        "edge_recall": ratio(tot["recall_hits"], tot["gold_edges"]),
        "core_recall": ratio(tot["core_hits"], tot["gold_core"]),
        "typed_recall": ratio(tot["typed_hits"], tot["gold_edges"]),
        "direction_accuracy": ratio(tot["direction_hits"], tot["gold_onkosul"]),
        # Judged precision ignores unjudged edges; strict precision counts them against the variant.
        "precision_judged": ratio(tot["correct"], tot["correct"] + tot["wrong"]),
        "precision_strict": ratio(tot["correct"], tot["out_center_edges"]),
        "evidence_share": ratio(tot["out_edges"] - tot["no_evidence"], tot["out_edges"]),
        "verified_share": ratio(tot["verified"], tot["out_edges"]),
        "verified_ok_share": ratio(tot["verified_ok"], tot["verified"]),
        "panel_complete_share": ratio(tot["panel_complete"], tot["nodes"]),
        "grade_correct_share": ratio(tot["grades_ok"], tot["nodes"]),
        "seconds_total": round(sum(times), 3),
        "seconds_mean": round(sum(times) / len(times), 3) if times else None,
        "seconds_max": max(times) if times else None,
    }
    agg["counts"] = tot
    return agg


def fmt(x):
    if x is None:
        return "-"
    if isinstance(x, float):
        return "%.2f" % x
    return str(x)


def print_summary(variant_name, per_query, agg):
    print("Benchmark: %s" % variant_name)
    head = "%-22s %-26s %5s %6s %6s %5s %5s %5s %5s %6s" % (
        "query", "center", "ok", "recall", "typed", "dir", "corr", "wrong", "unj", "sec")
    print(head)
    print("-" * len(head))
    for r in per_query:
        print("%-22s %-26s %5s %6s %6s %5s %5s %5s %5s %6s" % (
            r["q"][:22], r["center"][:26], "yes" if r["ok"] else "FAIL",
            "%d/%d" % (r["recall_hits"], r["gold_edges"]),
            "%d/%d" % (r["typed_hits"], r["gold_edges"]),
            "%d/%d" % (r["direction_hits"], r["gold_onkosul"]),
            r["correct"], r["wrong"], r["unjudged"], fmt(r["seconds"])))
        if r["error"]:
            print("    error: %s" % r["error"][:200])
    print("-" * len(head))
    c = agg["counts"]
    lines = [
        ("failures", "%d / %d" % (agg["failures"], agg["queries"])),
        ("center mismatches", agg["center_mismatches"]),
        ("edge recall", "%s  (%d/%d)" % (fmt(agg["edge_recall"]), c["recall_hits"], c["gold_edges"])),
        ("core recall", "%s  (%d/%d)" % (fmt(agg["core_recall"]), c["core_hits"], c["gold_core"])),
        ("typed recall", "%s  (%d/%d)" % (fmt(agg["typed_recall"]), c["typed_hits"], c["gold_edges"])),
        ("direction accuracy", "%s  (%d/%d)" % (fmt(agg["direction_accuracy"]), c["direction_hits"],
                                                c["gold_onkosul"])),
        ("precision (judged)", "%s  (correct %d, wrong %d)" % (fmt(agg["precision_judged"]), c["correct"],
                                                              c["wrong"])),
        ("precision (strict)", "%s  (unjudged %d of %d)" % (fmt(agg["precision_strict"]), c["unjudged"],
                                                           c["out_center_edges"])),
        ("edges with evidence", "%s  (%d/%d)" % (fmt(agg["evidence_share"]), c["out_edges"] - c["no_evidence"],
                                                 c["out_edges"])),
        ("verified quotes ok", "%s  (ok %d, fake %d)" % (fmt(agg["verified_ok_share"]), c["verified_ok"],
                                                        c["fake_verified"])),
        ("panel complete", "%s  (%d/%d nodes)" % (fmt(agg["panel_complete_share"]), c["panel_complete"],
                                                  c["nodes"])),
        ("grades correct", "%s  (%d/%d nodes)" % (fmt(agg["grade_correct_share"]), c["grades_ok"], c["nodes"])),
        ("time total / mean / max (s)", "%s / %s / %s" % (fmt(agg["seconds_total"]), fmt(agg["seconds_mean"]),
                                                          fmt(agg["seconds_max"]))),
    ]
    for k, v in lines:
        print("%-28s %s" % (k, v))


def check_gold(queries, gold_rel, topics):
    warnings = []
    for q in queries:
        if q["center"] not in gold_rel:
            warnings.append("query %r: center %r has no entry in relations.json" % (q["q"], q["center"]))
    for center, g in gold_rel.items():
        ids = {center} | set(g.get("irrelevant", []))
        for e in g["edges"]:
            ids |= {e["a"], e["b"]}
            if center not in (e["a"], e["b"]):
                warnings.append("%s: edge %s-%s does not touch the center" % (center, e["a"], e["b"]))
            if e["type"] == "onkosul" and {e.get("from"), e.get("to")} != {e["a"], e["b"]}:
                warnings.append("%s: onkosul edge %s-%s has bad from/to" % (center, e["a"], e["b"]))
        for i in sorted(ids):
            if topics and i not in topics:
                warnings.append("%s: id %r is not in corpus/topics.json" % (center, i))
    return warnings


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Score a variant's query.py against the blind gold.")
    ap.add_argument("variant_dir")
    ap.add_argument("--out", default=None, help="result JSON path (default benchmark/results/<variant>.json)")
    args = ap.parse_args(argv)

    variant_dir = os.path.abspath(args.variant_dir)
    variant_name = os.path.basename(os.path.normpath(variant_dir))
    if not os.path.isfile(os.path.join(variant_dir, "query.py")):
        print("error: %s has no query.py" % variant_dir, file=sys.stderr)
        return 2
    out_path = os.path.abspath(args.out or os.path.join(BENCH_DIR, "results", variant_name + ".json"))
    try:
        inside_variant = os.path.commonpath([out_path, variant_dir]) == variant_dir
    except ValueError:  # different drives on Windows
        inside_variant = False
    if inside_variant:
        print("error: refusing to write into the variant folder: %s" % out_path, file=sys.stderr)
        return 2

    queries = load_json(os.path.join(GOLD_DIR, "queries.json"))
    gold_rel = load_json(os.path.join(GOLD_DIR, "relations.json"))
    topics = {}
    if os.path.isfile(TOPICS_PATH):
        topics = {t["id"]: t.get("grades") for t in load_json(TOPICS_PATH)}
    else:
        print("warning: %s not found; grade checks will all fail" % TOPICS_PATH, file=sys.stderr)
    for w in check_gold(queries, gold_rel, topics):
        print("gold warning: " + w, file=sys.stderr)

    passages = PassageCache(variant_dir)
    per_query = []
    for q in queries:
        rec = run_query(variant_dir, q["q"])
        per_query.append(score_query(q, rec, gold_rel, topics, passages))

    agg = aggregate(per_query)
    result = {
        "variant": variant_name,
        "variant_dir": variant_dir,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "timeout_s": TIMEOUT_S,
        "aggregate": agg,
        "per_query": per_query,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print_summary(variant_name, per_query, agg)
    print("result written to %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
