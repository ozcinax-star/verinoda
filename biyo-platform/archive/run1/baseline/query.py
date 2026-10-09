"""Biyoloji konu grafı sorgusu: python query.py "<arama metni>" çalıştırıldığında SPEC JSON'unu stdout'a yazar.

Veri: ../corpus/topics.json, ../corpus/passages/*.md (salt okunur) ve data/ altındaki arama terimleri
ve ilişki kayıtları. İlişki kayıtlarındaki kanıt cümlesi pasajda gerçekten geçiyorsa "verified" olur.
"""
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT.parent / "corpus"
DATA = ROOT / "data"
# topics.json "passage" alanı biyo-platform kökünden göreli ("corpus/passages/...") yazılmış.

TYPE_ORDER = {"onkosul": 0, "destek": 1, "ortak": 2}
TYPE_TR = {"onkosul": "ön koşul", "destek": "destek", "ortak": "ortak kavram"}
TURKISH_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})


def fold(text):
    """Büyük/küçük harf ve Türkçe karakterleri (ş, ı, ğ ...) bir kenara bırakarak karşılaştırma metni üretir."""
    text = text.replace("İ", "i").replace("I", "ı").replace("-", " ")
    return " ".join(text.lower().translate(TURKISH_FOLD).split())


def word_in(needle, haystack):
    """needle, haystack içinde tek başına bir sözcük olarak geçiyor mu (fold edilmiş metinlerde)."""
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack) is not None


@lru_cache(maxsize=1)
def load_corpus():
    topics = json.loads((CORPUS / "topics.json").read_text(encoding="utf-8"))
    by_id = {}
    for t in topics:
        text = (ROOT.parent / t["passage"]).read_text(encoding="utf-8")
        body, _, sources = text.partition("## Kaynaklar")
        by_id[t["id"]] = {**t, "text": text, "body": body, "sources_text": sources}
    terms = json.loads((DATA / "terms.json").read_text(encoding="utf-8"))
    relations = json.loads((DATA / "relations.json").read_text(encoding="utf-8"))
    return by_id, terms, relations


def first_paragraph_summary(body):
    """Pasajın ilk paragrafının en fazla iki cümlesi; kendi metnimiz değil, pasajdan alınmış özet."""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    paragraph = next((ln for ln in lines if not ln.startswith("#")), "")
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    summary = " ".join(sentences[:2])
    return summary if len(summary) <= 320 else summary[:317].rsplit(" ", 1)[0] + "..."


def parse_sources(sources_text):
    """'- ad — tür' satırlarını okur; URL bilgisi pasajda yok, bu yüzden hepsi doğrulanmamış (url null)."""
    sources = []
    for line in sources_text.splitlines():
        m = re.match(r"^-\s+(.*?)\s+—\s+(.*)$", line.strip())
        if not m:
            continue
        ad, tur = m.group(1).strip(), m.group(2).strip()
        sources.append({"ad": ad, "tur": tur, "url": None, "verified": False})
    # MEB kaynakları EBA videosundan önce gelir (SPEC: MEB sources first).
    sources.sort(key=lambda s: (0 if s["ad"].startswith("MEB") else 1))
    return sources


def find_center(q, by_id, terms):
    """Sorguya en iyi uyan konu: tam eşleşme > sorgu, terimi içeriyor > terim, sorguyu içeriyor."""
    best = None
    for tid, topic in by_id.items():
        candidates = [fold(tid.replace("-", " ")), fold(topic["title"])] + [fold(t) for t in terms.get(tid, [])]
        for cand in candidates:
            if not cand:
                continue
            if q == cand:
                score = 3
            elif word_in(cand, q):
                score = 2
            elif word_in(q, cand) and len(q) >= 3:
                score = 1
            else:
                continue
            key = (score, len(cand))
            if best is None or key > best[0]:
                best = (key, tid)
    return best[1] if best else None


def levels(by_id, relations):
    """Ön koşul zincirindeki derinlik (ön koşulu olmayan konu 0). Döngüye karşı korumalı."""
    prereqs = {tid: [] for tid in by_id}
    for rel in relations:
        if rel["type"] == "onkosul" and rel["target"] in prereqs:
            prereqs[rel["target"]].append(rel["source"])
    memo = {}

    def depth(tid, stack=()):
        if tid in memo:
            return memo[tid]
        if tid in stack:
            return 0
        d = 0
        for p in prereqs.get(tid, []):
            if p in by_id:
                d = max(d, 1 + depth(p, stack + (tid,)))
        memo[tid] = d
        return d

    return {tid: depth(tid) for tid in by_id}, prereqs


def study_order(by_id, relations):
    """Tüm konular için genel çalışma sırası: önce sınıf, sonra ön koşul derinliği, sonra ad."""
    depth, prereqs = levels(by_id, relations)
    ranked = sorted(by_id, key=lambda tid: (min(by_id[tid]["grades"]), depth[tid], by_id[tid]["title"]))
    return {tid: i + 1 for i, tid in enumerate(ranked)}, prereqs


def check_edges(relations, by_id):
    """Her ilişki için kanıt cümlesi pasajda aranır. Bulunursa verified, bulunmazsa inference."""
    edges = []
    for rel in relations:
        if rel["source"] not in by_id or rel["target"] not in by_id:
            continue
        evidence = []
        for ev in rel.get("evidence", []):
            passage = by_id.get(ev["passage"])
            if passage and ev["quote"] in passage["text"]:
                evidence.append({"passage": passage["passage"], "quote": ev["quote"]})
        status = "verified" if evidence else "inference"
        edges.append({"source": rel["source"], "target": rel["target"], "type": rel["type"],
                      "evidence": evidence, "status": status})
    return edges


def node_payload(tid, by_id, order, prereqs, edges):
    topic = by_id[tid]
    uses = []
    for e in edges:
        if e["source"] == tid and e["type"] == "onkosul":
            uses.append(f"{by_id[e['target']]['title']} konusunu anlamak için gerekir")
        elif tid in (e["source"], e["target"]):
            other = e["target"] if e["source"] == tid else e["source"]
            uses.append(f"{by_id[other]['title']} ile {TYPE_TR[e['type']]} ilişkisi")
    uses = list(dict.fromkeys(uses))[:5] or ["Bu konu için ilişki kaydı yok"]
    prereq_titles = [by_id[p]["title"] for p in prereqs.get(tid, [])]
    why = "Önce şu konular: " + ", ".join(prereq_titles) + "." if prereq_titles else "Bu konunun kayıtlı bir ön koşulu yok."
    if topic.get("grade_note"):
        why += " " + topic["grade_note"]
    return {
        "id": tid,
        "title": topic["title"],
        "grades": topic["grades"],
        "summary": first_paragraph_summary(topic["body"]),
        "uses": uses,
        "study": {"order": order[tid], "why": why, "sources": parse_sources(topic["sources_text"])},
    }


def build_graph(query):
    """Veri sağlayıcının tek girişi. serve.py bu fonksiyonu çağırır."""
    by_id, terms, relations = load_corpus()
    result = {"query": query, "center": None, "nodes": [], "edges": [], "unknowns": []}
    q = fold(query or "")
    if not q:
        result["unknowns"].append("Arama metni boş.")
        return result
    center = find_center(q, by_id, terms)
    if center is None:
        result["unknowns"].append("Bu arama için bir konu bulunamadı. Konu adını ya da eş anlamlısını deneyin.")
        return result

    order, prereqs = study_order(by_id, relations)
    all_edges = check_edges(relations, by_id)
    edges = [e for e in all_edges if center in (e["source"], e["target"])]
    edges.sort(key=lambda e: (TYPE_ORDER[e["type"]], e["source"], e["target"]))

    neighbour_ids = []
    for e in edges:
        other = e["target"] if e["source"] == center else e["source"]
        if other not in neighbour_ids:
            neighbour_ids.append(other)
    node_ids = [center] + neighbour_ids

    result["center"] = center
    result["nodes"] = [node_payload(tid, by_id, order, prereqs, all_edges) for tid in node_ids]
    result["edges"] = edges

    if not edges:
        result["unknowns"].append("Bu konu için kayıtlı bir ilişki yok; corpus'ta komşu konu bulunamadı.")
    inference = [e for e in edges if e["status"] == "inference"]
    if inference:
        names = ", ".join(f"{by_id[e['source']]['title']} - {by_id[e['target']]['title']}" for e in inference)
        result["unknowns"].append(f"Şu ilişkilerin kanıt cümlesi pasajda bulunamadı (çıkarım): {names}.")
    result["unknowns"].append("Kaynakların URL'leri doğrulanmadı; ders kitabı ve EBA bağlantıları yok.")
    return result


def main(argv):
    if len(argv) < 2:
        print('Kullanım: python query.py "<arama metni>"', file=sys.stderr)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(build_graph(" ".join(argv[1:])), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
