"""Descriptive diagnostics for RESULTS.md (not pre-registered metrics): output pathologies per arm and paired
per-instance win/tie/loss counts on the primary views. Reads out/raw/*.json and out/scores.json."""
from __future__ import annotations

import json
import re
import statistics as st
import sys
from pathlib import Path

CB = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else CB / "out"
sys.path.insert(0, str(CB / "scripts"))
import views  # noqa: E402

scores = json.loads((OUT / "scores.json").read_text(encoding="utf-8"))
ids = [x["id"] for x in scores]
d = {"n": len(ids)}
raw = {i: json.loads((OUT / "raw" / f"{i}.json").read_text(encoding="utf-8")) for i in ids}


def cnt(pred):
    return sum(1 for i in ids if pred(i))


# Graphify
d["gq_truncated_notice"] = cnt(lambda i: "TRUNCATED: showing" in raw[i]["arms"]["gq"]["text"])
d["gq_over_budget_notice"] = cnt(lambda i: "Complete answer over budget" in raw[i]["arms"]["gq"]["text"])
d["gq_header_chars_median"] = st.median(len(raw[i]["arms"]["gq"]["text"].split("\n", 1)[0]) for i in ids)
d["gq_header_over_3000_chars"] = cnt(lambda i: len(raw[i]["arms"]["gq"]["text"].split("\n", 1)[0]) > 3000)
sc = {x["id"]: x for x in scores}
d["gq_capped_empty_but_uncapped_not"] = [i for i in ids if sc[i]["cells"]["gq|cited|capped"]["file"]["pred"] == 0
                                         and sc[i]["cells"]["gq|cited|uncapped"]["file"]["pred"] > 0]
d["gq_nodes_without_src"] = sum(len(re.findall(r"^NODE .*?\[src= loc=", raw[i]["arms"]["gq"]["text"], re.M)) for i in ids)
d["gq_nodes"] = sum(len(re.findall(r"^NODE ", raw[i]["arms"]["gq"]["text"], re.M)) for i in ids)
md_share = []
for i in ids:
    v = views.graphify_shown(raw[i]["arms"]["gq"]["text"][:6000])
    if v:
        md_share.append(sum(1 for f in v if not re.search(r"\.(py|js|jsx|ts|tsx|go|java|rs|c|h|cc|cpp|hpp|hh|cxx|mjs|cjs|svelte|vue|kt|scala)$", f)) / len(v))
d["gq_capped_share_of_non_code_files_mean"] = st.mean(md_share) if md_share else None
# Verinoda analyze
d["va_nonzero"] = {i: raw[i]["arms"]["va"]["rc"] for i in ids if raw[i]["arms"]["va"]["rc"] != 0}
d["va_nonzero_with_locations_in_stdout"] = [i for i in d["va_nonzero"]
                                            if views.views("va", raw[i]["arms"]["va"]["raw_stdout"])["cited"]]
d["va_nothing_analysed"] = cnt(lambda i: "nothing was analysed" in raw[i]["arms"]["va"]["raw_stdout"])
d["va_passages_start_median_char"] = st.median(
    raw[i]["arms"]["va"]["text"].find("\npassages") for i in ids if "\npassages" in raw[i]["arms"]["va"]["text"])
d["va_passages_after_6000"] = cnt(lambda i: raw[i]["arms"]["va"]["text"].find("\npassages") > 6000)
d["va_no_passages"] = cnt(lambda i: raw[i]["arms"]["va"]["text"] and "\npassages" not in raw[i]["arms"]["va"]["text"])
d["va_understood_as_turkish"] = [i for i in ids if re.search(r"^understood as: Anla", raw[i]["arms"]["va"]["raw_stdout"], re.M)]
d["va_chars_median"] = st.median(len(raw[i]["arms"]["va"]["text"]) for i in ids)
d["question_chars_median"] = st.median(len(raw[i]["question"]) for i in ids)
# Verinoda query
d["vq_nonzero"] = {i: raw[i]["arms"]["vq"]["rc"] for i in ids if raw[i]["arms"]["vq"]["rc"] != 0}
d["vq_chars_median"] = st.median(len(raw[i]["arms"]["vq"]["text"]) for i in ids)
d["vq_shown_lines_median"] = st.median(sum(len(s) for s in views.verinoda_shown(raw[i]["arms"]["vq"]["text"]).values()) for i in ids)
d["vq_cited_lines_median"] = st.median(sum(len(s) for s in views.views("vq", raw[i]["arms"]["vq"]["text"])["cited"].values()) for i in ids)
d["bm25_shown_lines_median"] = st.median(sum(len(s) for s in views.verinoda_shown(raw[i]["arms"]["bm25"]["text"]).values()) for i in ids)
d["gq_nonzero"] = {i: raw[i]["arms"]["gq"]["rc"] for i in ids if raw[i]["arms"]["gq"]["rc"] != 0}


# paired per-instance comparisons on the primary views
def per(i, key, g, what):
    c = sc[i]["cells"][key][g]
    if what == "recall":
        return c["inter"] / c["gold"] if c["gold"] else None
    r = c["inter"] / c["gold"] if c["gold"] else 0.0
    p = c["inter"] / c["pred"] if c["pred"] else 0.0
    return 2 * p * r / (p + r) if p + r else 0.0


pairs = {}
for (view, g, what) in (("cited", "file", "recall"), ("cited", "span", "recall"), ("shown", "span", "f1"),
                        ("cited", "file", "f1")):
    for a, b in (("vq", "bm25"), ("vq", "gq"), ("va", "vq"), ("gq", "bm25")):
        w = t = l = 0
        for i in ids:
            x, y = per(i, f"{a}|{view}|capped", g, what), per(i, f"{b}|{view}|capped", g, what)
            if x is None or y is None:
                continue
            if abs(x - y) < 1e-9:
                t += 1
            elif x > y:
                w += 1
            else:
                l += 1
        pairs[f"{view} {g} {what}: {a} vs {b}"] = {"wins": w, "ties": t, "losses": l}
d["paired_capped"] = pairs
# instances with zero file recall (cited, capped) per arm
d["zero_file_recall_cited_capped"] = {a: cnt(lambda i, a=a: per(i, f"{a}|cited|capped", "file", "recall") == 0)
                                      for a in ("vq", "va", "gq", "bm25")}
d["any_arm_hits_file_cited_capped"] = cnt(lambda i: any(per(i, f"{a}|cited|capped", "file", "recall") for a in ("vq", "va", "gq", "bm25")))
(OUT / "diagnostics.json").write_text(json.dumps(d, indent=1), encoding="utf-8")
print(json.dumps(d, indent=1))
