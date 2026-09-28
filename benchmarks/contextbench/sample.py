"""Pre-registered stratified sample of ContextBench contextbench_verified (seed 20260928, 10 per language).

Order: one random.Random(20260928); for each language in sorted order, the language's original_inst_ids sorted
lexicographically are shuffled with rng.sample (the RNG is consumed in sorted-language order). Walk that order and
accept an instance unless its depth-1 checkout would exceed 1.5 GB (sum of blob sizes in the GitHub git-trees
listing at base_commit; if the listing is truncated, the repository's GitHub size is used instead). The first 10
accepted per language form the sample; the rest of each order is kept as the replacement queue (runtime failures
of the fetch are replaced by the next instance in that queue).
Writes sample.json: {"sample": [...], "queue": {lang: [...]}, "skips": [...], "sizes": {...}}."""
import json, random, subprocess, sys
from pathlib import Path
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent.parent
SEED, PER_LANG, LIMIT = 20260928, 10, 1_500_000_000
rows = pq.read_table(HERE / "data/contextbench_verified.parquet").to_pylist()
by = {}
for r in rows:
    by.setdefault(r["language"], []).append(r)
rng = random.Random(SEED)
order = {}
for lang in sorted(by):
    ids = sorted(r["original_inst_id"] for r in by[lang])
    order[lang] = rng.sample(ids, len(ids))
R = {r["original_inst_id"]: r for r in rows}


def slug(url):
    s = url.split("github.com/")[1]
    return s[:-4] if s.endswith(".git") else s


def checkout_size(r):
    s = slug(r["repo_url"])
    out = subprocess.run(["gh", "api", f"repos/{s}/git/trees/{r['base_commit']}?recursive=1"],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        return None, "trees api failed: " + out.stderr.strip()[:200]
    d = json.loads(out.stdout)
    if d.get("truncated"):
        o = subprocess.run(["gh", "api", f"repos/{s}", "--jq", ".size"], capture_output=True, text=True)
        return int(o.stdout.strip()) * 1024, "tree listing truncated; GitHub repository size used"
    return sum(e.get("size", 0) for e in d["tree"] if e.get("type") == "blob"), "trees api"


sample, queue, skips, sizes = [], {}, [], {}
for lang in sorted(order):
    acc = []
    for i, iid in enumerate(order[lang]):
        if len(acc) == PER_LANG:
            queue[lang] = order[lang][i:]
            break
        size, how = checkout_size(R[iid])
        sizes[iid] = {"bytes": size, "how": how}
        if size is None or size > LIMIT:
            skips.append({"id": iid, "lang": lang, "reason": f"checkout size {size} ({how}) over 1.5 GB" if size else how})
            continue
        acc.append(iid)
    else:
        queue[lang] = []
    sample += [{"id": iid, "lang": lang, "repo_url": R[iid]["repo_url"], "commit": R[iid]["base_commit"],
                "checkout_bytes": sizes[iid]["bytes"]} for iid in acc]
    print(lang, len(acc), acc, file=sys.stderr)
(HERE / "sample.json").write_text(json.dumps({"seed": SEED, "per_lang": PER_LANG, "sample": sample, "queue": queue,
                                               "skips": skips, "sizes": sizes}, indent=1), encoding="utf-8")
print(len(sample), "sampled;", len(skips), "skipped")
