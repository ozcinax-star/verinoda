"""The 2026-10-02 ContextBench rerun against the 2026-09-28 run, instance by instance on the instances both have.

The rerun was stopped by its runner's time limit, twice, after 40 of the 80 pre-registered instances (the runner takes
the largest checkouts first, so these 40 are not a stratified sample of the 80; every language is present). Both runs
are scored by the same evaluator on the same instances; the differences are the tools: Verinoda 3f0e72c -> 5999912,
Graphify 0.9.69 -> 0.9.73. BM25 is the same code on the same checkouts and is the control.

    python benchmarks/results/contextbench-2026-10-02/compare.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLD = HERE.parent / "contextbench-2026-09-28" / "results.json"
NEW = HERE / "scores.json"
ARMS = (("vq", "Verinoda query"), ("va", "Verinoda analyze"), ("gq", "Graphify query"), ("bm25", "BM25 (control)"))


def metric(cell: dict, gran: str) -> tuple[float, int, int, int]:
    c = cell[gran]
    return (c["inter"] / c["gold"] if c["gold"] else 0.0), c["inter"], c["gold"], c["pred"]


def summarize(rows: list[dict], key: str) -> dict:
    out = {}
    for gran in ("file", "span"):
        per = [metric(r["cells"][key], gran) for r in rows]
        inter, gold, pred = (sum(p[i] for p in per) for i in (1, 2, 3))
        rec = inter / gold if gold else 0.0
        prec = inter / pred if pred else 0.0
        out[gran] = {"recall_per_instance": round(sum(p[0] for p in per) / len(per), 3), "recall_micro": round(rec, 3),
                     "precision_micro": round(prec, 3), "f1_micro": round(2 * rec * prec / (rec + prec), 3) if rec + prec else 0.0}
    out["chars_mean"] = round(sum(r["cells"][key]["chars"] for r in rows) / len(rows))
    return out


def main() -> None:
    old = {r["id"]: r for r in json.loads(OLD.read_text(encoding="utf-8"))["per_instance"]}
    new = {r["id"]: r for r in json.loads(NEW.read_text(encoding="utf-8"))}
    ids = sorted(set(old) & set(new))
    langs = Counter(new[i]["lang"] for i in ids)
    lines = [f"Instances in both runs: {len(ids)} ({', '.join(f'{k} {v}' for k, v in sorted(langs.items()))}).", "",
             "Cited view, first 6,000 characters (the primary comparison of 2026-09-28):", "",
             ("| approach | file recall per instance old -> new | file recall micro old -> new | cited span F1 old -> new | "
              "paired per instance (file recall): better / same / worse |"), "|---|---|---|---|---|"]
    result = {"instances": ids, "by_language": dict(langs), "arms": {}}
    for arm, name in ARMS:
        key = f"{arm}|cited|capped"
        so, sn = summarize([old[i] for i in ids], key), summarize([new[i] for i in ids], key)
        d = [metric(new[i]["cells"][key], "file")[0] - metric(old[i]["cells"][key], "file")[0] for i in ids]
        better, same, worse = sum(x > 1e-9 for x in d), sum(abs(x) <= 1e-9 for x in d), sum(x < -1e-9 for x in d)
        result["arms"][arm] = {"old": so, "new": sn, "paired_file_recall": [better, same, worse]}
        lines.append(f"| {name} | {so['file']['recall_per_instance']} -> {sn['file']['recall_per_instance']} | "
                     f"{so['file']['recall_micro']} -> {sn['file']['recall_micro']} | {so['span']['f1_micro']} -> "
                     f"{sn['span']['f1_micro']} | {better} / {same} / {worse} |")
    text = "\n".join(lines) + "\n"
    print(text)
    (HERE / "compare.md").write_bytes(text.encode("utf-8"))
    (HERE / "compare.json").write_bytes((json.dumps(result, indent=1) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
