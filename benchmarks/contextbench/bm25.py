"""Lexical baseline (DESIGN.md section 5): BM25 over tracked code files, best 30-line window per top file,
rendered as '## PATH:A-B' + '<n> <text>' lines up to a 6,000-char budget."""
from __future__ import annotations

import math
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

EXTS = set("py pyi pyx pxd js jsx mjs cjs ts tsx mts cts vue svelte go java kt kts scala groovy rs c h cc cpp cxx "
           "c++ hpp hh hxx h++ inl ipp tcc cs rb php swift m mm pony jq sh".split())
MAX_BYTES = 1_000_000
K1, B = 1.2, 0.75
WINDOW = 30
BUDGET = 6000
LINE_CUT = 200
MIN_TAIL = 5
STOP = set("""a an the and or but if then else when while of to in on at by for with from into onto over under as is
are was were be been being am do does did done doing have has had having not no nor so than too very can could should
would will shall may might must this that these those there here it its it's i me my we our you your he she him her
they them their what which who whom whose why how all any both each few more most other some such only own same just
also about above after again against below between before during out off up down once further s t don now ll re ve
d m o y ain isn aren wasn weren hasn haven hadn doesn didn won wouldn shouldn couldn mustn needn shan mightn let get
got use used using like one two e g eg ie etc via per yes please thanks thank hi hello""".split())
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def tokens(text: str) -> list[str]:
    out = []
    for m in IDENT.finditer(text):
        w = m.group(0)
        low = w.lower()
        if len(low) >= 2 and low not in STOP:
            out.append(low)
        if "_" in w or PARTS.findall(w) != [w]:
            for p in PARTS.findall(w):
                pl = p.lower()
                if pl != low and len(pl) >= 2 and pl not in STOP:
                    out.append(pl)
    return out


def list_files(root: Path) -> list[str]:
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
    files = [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]
    return sorted(f for f in files if "." in f.rsplit("/", 1)[-1] and f.rsplit(".", 1)[-1].lower() in EXTS)


def build(root: Path) -> dict:
    t0 = time.perf_counter()
    docs = {}
    for rel in list_files(root):
        p = root / rel
        try:
            if not p.is_file() or p.stat().st_size > MAX_BYTES:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", "replace")
        docs[rel] = (text, Counter(tokens(rel) + tokens(text)))
    df = Counter()
    for _, tf in docs.values():
        df.update(tf.keys())
    n = len(docs)
    avgdl = (sum(sum(tf.values()) for _, tf in docs.values()) / n) if n else 0.0
    return {"docs": docs, "df": df, "n": n, "avgdl": avgdl, "index_s": time.perf_counter() - t0}


def idf(ix: dict, term: str) -> float:
    d = ix["df"].get(term, 0)
    return math.log(1 + (ix["n"] - d + 0.5) / (d + 0.5))


def query(ix: dict, question: str, budget: int = BUDGET) -> tuple[str, dict]:
    t0 = time.perf_counter()
    q = sorted(set(tokens(question)))
    idfs = {t: idf(ix, t) for t in q if ix["df"].get(t)}
    scores = []
    for rel, (text, tf) in ix["docs"].items():
        dl = sum(tf.values())
        s = 0.0
        for t, w in idfs.items():
            f = tf.get(t, 0)
            if f:
                s += w * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / (ix["avgdl"] or 1)))
        if s > 0:
            scores.append((-s, rel))
    scores.sort()
    out: list[str] = []
    used = 0
    files_used = []
    for neg, rel in scores:
        lines = ix["docs"][rel][0].splitlines()
        if not lines:
            continue
        a, b = best_window(lines, idfs)
        block = [f"## {rel}:{a}-{b}"] + [f"{n} {lines[n - 1].rstrip()[:LINE_CUT]}" for n in range(a, b + 1)]
        size = sum(len(x) + 1 for x in block)
        if used + size <= budget:
            out += block
            used += size
            files_used.append(rel)
            continue
        # cut the block to the lines that fit (header + >= MIN_TAIL lines), then stop
        keep = [block[0]]
        room = budget - used - (len(block[0]) + 1)
        for x in block[1:]:
            if room - (len(x) + 1) < 0:
                break
            keep.append(x)
            room -= len(x) + 1
        if len(keep) - 1 >= MIN_TAIL:
            last = int(keep[-1].split(" ", 1)[0])
            keep[0] = f"## {rel}:{a}-{last}"
            out += keep
            files_used.append(rel)
        break
    text = "\n".join(out)
    return text, {"query_terms": len(q), "matched_terms": len(idfs), "ranked_files": len(scores),
                  "files_shown": len(files_used), "query_s": time.perf_counter() - t0}


def best_window(lines: list[str], idfs: dict) -> tuple[int, int]:
    n = len(lines)
    if n <= WINDOW:
        return 1, n
    line_terms = []
    for ln in lines:
        ts = set(tokens(ln))
        line_terms.append({t for t in ts if t in idfs})
    best, best_s = 1, -1.0
    for s in range(1, n - WINDOW + 2):
        present = set()
        for i in range(s - 1, s - 1 + WINDOW):
            present |= line_terms[i]
        sc = sum(idfs[t] for t in present)
        if sc > best_s:
            best, best_s = s, sc
    if best_s <= 0:
        return 1, WINDOW
    return best, best + WINDOW - 1
