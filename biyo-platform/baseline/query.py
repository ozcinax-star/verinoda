"""Turns a search text into the SPEC graph JSON: python query.py "<arama metni>".

Matching is plain text on corpus/passages: a search text that names a topic (id, title or a
key derived from it) becomes the centre; any other text is looked up in the passages and the
passage with the most mentions becomes the centre.

Relations come from sentences: a sentence that names two topics is a candidate edge between
them, and the sentence is the evidence quote. Direction and type come from cue words in that
sentence (ordering words for onkosul, use words for destek); without a cue the edge is ortak.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"

MAX_NEIGHBOURS = 12
FOLD = str.maketrans({"ı": "i", "ç": "c", "ğ": "g", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})

# Title words too common in the corpus to be a topic key on their own.
GENERIC = {"hucre", "madde", "genetik", "protein", "populasyon", "canlilarin", "bitkilerde", "endokrin", "sistem", "solunum"}
# Keys that name a topic in the prose without its title (keys are folded). Each one is a word
# that the passages use for the topic; they were checked against the passage text only.
EXTRA_KEYS = {
    "dna-replikasyonu": ["eslen", "replikasyon"],
    "atp-enerji": ["atp"],
    "enzimler": ["enzim"],
    "hucresel-solunum": ["fermantasyon", "oksijenli solunum"],
    "karbonhidratlar": ["karbonhidrat"],
    "endokrin-sistem": ["hormon"],
    "mendel-kalitimi": ["mendel"],
}
# Turkish synonyms that name a topic directly (keys are folded).
SYNONYMS = {
    "mitozis": "mitoz",
    "mayozis": "mayoz",
    "ozmoz": "difuzyon-ve-osmoz",
    "difuzyon": "difuzyon-ve-osmoz",
    "replikasyon": "dna-replikasyonu",
    "kalitim": "mendel-kalitimi",
    "solunum": "hucresel-solunum",
    "enerji": "atp-enerji",
    "hucre bolunmesi": "mitoz",
}

# "oncesinde" / "onceden" after a topic: the topic is the later one of the two. A bare "once"
# is left out: in the prose it mostly marks a sequence of steps, not a learning order.
ORDER_CUE = re.compile(r"\boncesi|\bonceden\b")
ORDER_GAP = 25
SUPPORT_CUE = re.compile(r"kullan|sagla|tasi|uret|yardim|destek|uygula|duzenle|saglar")
TYPE_RANK = {"onkosul": 3, "destek": 2, "ortak": 1}
# Kaynak bağlantıları: yalnızca açılarak doğrulanmış resmi adresler (aynı tablo symbiosis/query.py'de). Diğerleri null.
SOURCE_LINKS = {
    "MEB Biyoloji Dersi Öğretim Programı": "https://mufredat.meb.gov.tr/Dosyalar/202651815105221-biyolojid%C3%B6p.pdf",
    "EBA ders videosu": "https://www.eba.gov.tr",
}


def fold(text):
    return text.replace("I", "i").replace("İ", "i").lower().translate(FOLD)


def split_passage(text):
    body, _, sources_part = text.partition("## Kaynaklar")
    sources = []
    for line in sources_part.splitlines():
        match = re.match(r"\s*-\s*(.+?)\s+—\s+(.+)$", line)
        if match:
            name = match.group(1).strip()
            url = next((u for prefix, u in SOURCE_LINKS.items() if name.startswith(prefix)), None)
            sources.append({"ad": name, "tur": match.group(2).strip(), "url": url, "verified": url is not None})
    flat = " ".join(line.strip() for line in body.splitlines() if line.strip() and not line.startswith("#"))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", flat) if s]
    return flat, sentences, sources


def topic_keys(topic):
    base = fold(re.sub(r"\(.*?\)", "", topic["title"])).strip()
    keys = {fold(topic["title"]), base, fold(topic["id"].replace("-", " "))}
    first = base.split()[0] if base else ""
    if len(first) >= 5 and first not in GENERIC:
        keys.add(first)
    keys.update(EXTRA_KEYS.get(topic["id"], []))
    return {k for k in keys if k}


def key_spans(folded, keys):
    """(start, end) of the first hit of each key, prefix-matched at a word start."""
    spans = []
    for key in keys:
        match = re.search(r"(?<![a-z0-9])" + re.escape(key), folded)
        if match:
            spans.append((match.start(), match.end()))
    return spans


def load_topics():
    topics = json.loads((CORPUS / "topics.json").read_text(encoding="utf-8"))
    for topic in topics:
        flat, topic["sentences"], topic["sources"] = split_passage((ROOT / topic["passage"]).read_text(encoding="utf-8"))
        topic["flat"] = flat
        topic["keys"] = topic_keys(topic)
    return topics


def direction(folded, a, b, spans):
    """Return (type, source_id, target_id) for a sentence that names topics a and b.

    spans maps each topic id to its (start, end) in the folded sentence. An ordering cue right
    after one topic makes that topic the later one (the other is the prerequisite).
    """
    cue = ORDER_CUE.search(folded)
    if cue:
        before = [t for t in (a, b) if spans[t["id"]][1] <= cue.start() and cue.start() - spans[t["id"]][1] <= ORDER_GAP]
        if before:
            later = max(before, key=lambda t: spans[t["id"]][1])
            earlier = b if later is a else a
            return "onkosul", earlier["id"], later["id"]
    first, second = sorted((a, b), key=lambda t: spans[t["id"]][0])
    kind = "destek" if SUPPORT_CUE.search(folded) else "ortak"
    return kind, first["id"], second["id"]


def build_pairs(topics):
    """All sentence-level candidate edges, keyed by the sorted pair of topic ids."""
    pairs = {}
    for owner in topics:
        for sentence in owner["sentences"]:
            folded = fold(sentence)
            named = []
            for topic in topics:
                spans = key_spans(folded, topic["keys"])
                if spans:
                    named.append((topic, spans))
            if len(named) < 2:
                continue
            span_of = {t["id"]: (min(s for s, _ in sp), max(e for _, e in sp)) for t, sp in named}
            for i, (a, _) in enumerate(named):
                for b, _ in named[i + 1:]:
                    kind, source, target = direction(folded, a, b, span_of)
                    key = tuple(sorted((a["id"], b["id"])))
                    pairs.setdefault(key, []).append({
                        "type": kind,
                        "source": source,
                        "target": target,
                        "passage": owner["passage"],
                        "quote": sentence,
                        "passage_flat": owner["flat"],
                    })
    return pairs


def best_edge(candidates):
    """One edge per pair: the strongest type, then the pair with most sentences, then the first sentence."""
    best = max(candidates, key=lambda c: TYPE_RANK[c["type"]])
    same = [c for c in candidates if c["type"] == best["type"] and c["source"] == best["source"]]
    evidence = [{"passage": c["passage"], "quote": c["quote"]} for c in same[:2]]
    status = "verified" if all(c["quote"] in c["passage_flat"] for c in same) else "inference"
    return {"source": best["source"], "target": best["target"], "type": best["type"],
            "evidence": evidence, "status": status, "support": len(candidates)}


def find_centre(topics, query):
    q = fold(query).strip()
    if not q:
        return None
    for topic in topics:
        if q in topic["keys"] or q == topic["id"]:
            return topic
    if q in SYNONYMS:
        return next(t for t in topics if t["id"] == SYNONYMS[q])
    return None


def topic_graph(topics, pairs, centre):
    scored = []
    for key, candidates in pairs.items():
        if centre["id"] not in key:
            continue
        other_id = key[0] if key[1] == centre["id"] else key[1]
        edge = best_edge(candidates)
        scored.append((TYPE_RANK[edge["type"]], edge["support"], other_id, edge))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    scored = scored[:MAX_NEIGHBOURS]
    by_id = {t["id"]: t for t in topics}
    neighbours = [by_id[o] for _, _, o, _ in scored]
    edges = [e for _, _, _, e in scored]
    for e in edges:
        del e["support"]
    return neighbours, edges


def term_graph(topics, term):
    hits = []
    for topic in topics:
        sentences = [s for s in topic["sentences"] if term in fold(s)]
        if sentences:
            hits.append((topic, sentences))
    if not hits:
        return None, [], []
    hits.sort(key=lambda h: -len(h[1]))
    centre, _ = hits[0]
    neighbours = hits[1:MAX_NEIGHBOURS + 1]
    edges = []
    for topic, sentences in neighbours:
        edges.append({
            "source": topic["id"],
            "target": centre["id"],
            "type": "ortak",
            "evidence": [{"passage": topic["passage"], "quote": sentences[0]}],
            "status": "verified" if sentences[0] in topic["flat"] else "inference",
        })
    return centre, [t for t, _ in neighbours], edges


def summary_of(topic):
    return " ".join(topic["sentences"][:2])


def uses_of(topic, topics, term=None):
    if term:
        return [s[:220] for s in topic["sentences"] if term in fold(s)][:3]
    uses = []
    for other in topics:
        if other is topic:
            continue
        for sentence in other["sentences"]:
            if key_spans(fold(sentence), topic["keys"]):
                uses.append(f"{other['title']}: {sentence[:200]}")
                break
    return uses[:3]


def node_for(topic, topics, term, role_order, why):
    return {
        "id": topic["id"],
        "title": topic["title"],
        "grades": topic["grades"],
        "summary": summary_of(topic),
        "uses": uses_of(topic, topics, term),
        "study": {"order": role_order, "why": why, "sources": sorted(topic["sources"], key=lambda s: "MEB" not in s["ad"] + s["tur"])},
    }


def build(query):
    topics = load_topics()
    result = {"query": query, "center": None, "nodes": [], "edges": [], "unknowns": []}
    if not fold(query).strip():
        result["unknowns"].append("Arama metni boş.")
        return result

    term = None
    centre = find_centre(topics, query)
    if centre is not None:
        pairs = build_pairs(topics)
        neighbours, edges = topic_graph(topics, pairs, centre)
    else:
        term = fold(query).strip()
        centre, neighbours, edges = term_graph(topics, term)
        if centre is None:
            result["unknowns"].append(f"'{query}' için derste ya da konu başlığında eşleşme bulunamadı.")
            return result

    result["center"] = centre["id"]
    nodes = [centre] + neighbours
    prerequisites = {e["source"] for e in edges if e["type"] == "onkosul" and e["target"] == centre["id"]}
    dependents = {e["target"] for e in edges if e["type"] == "onkosul" and e["source"] == centre["id"]}
    for topic in nodes:
        if topic is centre:
            order, why = (2 if prerequisites else 1), "Aranan konu."
        elif topic["id"] in prerequisites:
            order, why = 1, "Aranan konudan önce çalışılması gereken ön koşul."
        elif topic["id"] in dependents:
            order, why = 3, "Aranan konuyu öğrendikten sonra çalışılabilecek konu."
        else:
            order, why = 4, "Aranan konuyla ilişkili konu."
        result["nodes"].append(node_for(topic, topics, term, order, why))
    result["edges"] = edges

    if not edges:
        result["unknowns"].append("Bu konu için corpus'ta ilişki cümlesi bulunamadı.")
    for topic in nodes:
        if topic.get("grade_note"):
            result["unknowns"].append(f"{topic['title']}: sınıf bilgisi doğrulanmadı ({topic['grade_note']})")
    onkosul = sum(1 for e in edges if e["type"] == "onkosul")
    result["unknowns"].append(
        f"İlişki türü ve yönü cümledeki ipuçlarından kural tabanlı atanır ({onkosul} onkosul kenarı); "
        "alıntı corpus'tandır ama yön ve tür uzman tarafından doğrulanmamıştır."
    )
    return result


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(build(query), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
