"""Biyoloji konu grafı sorgusu: `python query.py "<arama metni>"` tek bir SPEC JSON nesnesi basar.

Veri: kb/corpus (topics.json ve passages/). Yalnızca standart kütüphane kullanılır.
İlişkiler pasajlardaki düz cümlelerden çıkarılır. Üç kanıt düzeyi vardır:
  - cümle düzeyi, "öncesinde" ya da destek kalıbıyla: onkosul ya da destek, status "verified"
    (kanıt cümlesi iki konunun terimini ve kalıbı birlikte taşır).
  - cümle düzeyi, yalnızca birlikte anma: tür "ortak", status "verified" (kanıt iki terimi de taşır).
  - paragraf düzeyi, iki konu aynı paragrafta ama ayrı cümlelerde: tür "ortak", status "inference"
    (kanıt, öbür konuyu anan cümledir; ilişkinin kendisi aynen yazılmamıştır).
Yön yalnızca "X öncesinde Y" kalıbıyla verilir (onkosul); kalıp yoksa yön tahmin edilmez.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
KB = HERE / "kb"
MAX_NEIGHBOURS = 16

# Konu kimliği/başlığının dışındaki eş anlamlılar ve pasajda sık geçen terimler (arama için).
ALIASES = {
    "canlilarin-ortak-ozellikleri": ["canlıların ortak", "canlılık özellik", "canlı özellik"],
    "karbonhidratlar": ["karbonhidrat", "glikoz", "nişasta", "selüloz"],
    "proteinler": ["protein", "amino asit"],
    "lipitler": ["lipit", "yağlar"],
    "nukleik-asitler": ["nükleik asit", "dna", "rna"],
    "hucre-organelleri": ["organel", "mitokondri", "ribozom", "endoplazmik"],
    "hucre-zari-madde-gecisi": ["hücre zarı", "madde geçişi", "aktif taşıma", "pasif taşıma"],
    "difuzyon-ve-osmoz": ["difüzyon", "ozmoz", "osmoz"],
    "enzimler": ["enzim"],
    "atp-enerji": ["atp", "adenozin"],
    "fotosentez": ["fotosentez", "klorofil", "kloroplast"],
    "hucresel-solunum": ["hücresel solunum", "glikoliz", "krebs", "oksijenli solunum"],
    "mitoz": ["mitoz", "mitotik"],
    "mayoz": ["mayoz", "gamet", "eşey hücre"],
    "mendel-kalitimi": ["mendel", "çaprazlama", "baskın", "çekinik", "genotip", "fenotip"],
    "dna-replikasyonu": ["replikasyon", "dna eşlen", "eşlenme"],
    "protein-sentezi": ["protein sentez", "transkripsiyon", "translasyon"],
    "mutasyon": ["mutasyon", "mutant"],
    "genetik-muhendisligi": ["genetik mühendis", "rekombinant", "transgenik"],
    "evrim": ["evrim", "doğal seçilim", "adaptasyon"],
    "ekosistem": ["ekosistem", "biyotik", "abiyotik"],
    "madde-dongusu": ["madde döngü", "karbon döngü", "azot döngü"],
    "populasyon-ekolojisi": ["popülasyon", "populasyon"],
    "bitkilerde-su-tasinimi": ["ksilem", "floem", "terleme"],
    "sinir-sistemi": ["sinir sistem", "nöron", "sinaps"],
    "endokrin-sistem": ["endokrin", "hormon"],
    "dolasim-sistemi": ["dolaşım sistem", "kalp", "damar"],
    "solunum-sistemi": ["solunum sistem", "akciğer", "soluk"],
    "sindirim-sistemi": ["sindirim", "mide", "bağırsak"],
    "bosaltim-sistemi": ["boşaltım", "böbrek", "nefron"],
    "bagisiklik-sistemi": ["bağışıklık", "antikor", "lenfosit"],
}

# Kaynak bağlantıları: yalnızca bu sürümde açılarak doğrulanmış resmi adresler. Başka kaynaklar için adres yoktur
# (uydurulmaz). Anahtar, corpus'taki kaynak adının başıdır.
SOURCE_LINKS = {
    "MEB Biyoloji Dersi Öğretim Programı": "https://mufredat.meb.gov.tr/Dosyalar/202651815105221-biyolojid%C3%B6p.pdf",
    "EBA ders videosu": "https://www.eba.gov.tr",
}

# Yalnızca ilişki eşleştirmesi için ek terimler: pasajlarda geçen, konuya özgü kavramlar.
# Sorgu eşleştirmesine (pick_center) girmez, böylece arama davranışı değişmez.
EDGE_TERMS = {
    "canlilarin-ortak-ozellikleri": ["metabolizma", "homeostazi", "anabolizma", "katabolizma"],
    "karbonhidratlar": ["glikojen", "monosakkarit", "disakkarit", "sükroz"],
    "proteinler": ["peptit", "keratin", "kollajen", "temel amino"],
    "lipitler": ["yağ asit", "trigliserit", "fosfolipit", "steroit", "kolesterol"],
    "hucre-organelleri": ["golgi", "lizozom", "sentrozom", "hücre duvarı"],
    "hucre-zari-madde-gecisi": ["seçici geçirgen", "endositoz", "ekzositoz", "fagositoz", "sodyum-potasyum",
                                "taşıyıcı protein", "akıcı mozaik"],
    "difuzyon-ve-osmoz": ["turgor", "plazmoliz", "hemoliz", "hipotonik", "hipertonik", "izotonik"],
    "enzimler": ["aktivasyon enerjisi", "substrat", "denatürasyon", "kofaktör", "koenzim", "katalizör",
                 "amilaz", "lipaz", "pepsin", "tripsin"],
    "atp-enerji": ["adp", "fosforilasyon", "kemosentez"],
    "fotosentez": ["calvin", "tilakoit", "ışığa bağımlı", "ışıktan bağımsız"],
    "hucresel-solunum": ["fermantasyon", "pirüvik", "elektron taşıma", "laktik asit"],
    "mitoz": ["iğ iplik", "sitokinez", "telofaz", "anafaz", "metafaz", "profaz"],
    "mayoz": ["krossing over", "tetrat", "haploit", "diploit", "homolog kromozom"],
    "mendel-kalitimi": ["alel", "homozigot", "heterozigot", "kontrol çaprazlama", "ayrılma ilkesi"],
    "dna-replikasyonu": ["helikaz", "dna polimeraz", "yarı korunumlu", "dna ligaz", "dna eşlen"],
    "protein-sentezi": ["kodon", "antikodon", "mrna", "trna", "rna polimeraz", "transkripsiyon"],
    "mutasyon": ["mutajen", "orak hücre", "kromozom mutasyon", "gen mutasyon"],
    "genetik-muhendisligi": ["plazmid", "restriksiyon", "crispr", "pcr", "gdo", "rekombinant dna"],
    "evrim": ["yapay seçilim", "antibiyotik", "homolog organ", "mutasyon"],
    "ekosistem": ["besin zinciri", "besin ağı", "üretici", "tüketici", "ayrıştırıcı", "biyolojik birikim",
                  "komünite"],
    "madde-dongusu": ["nitrifikasyon", "denitrifikasyon", "azot bağlayıcı", "fosil yakıt", "sera etkisi"],
    "populasyon-ekolojisi": ["taşıma kapasitesi", "lojistik büyüme", "üstel büyüme", "popülasyon yoğunluğu"],
    "bitkilerde-su-tasinimi": ["ksilem", "floem", "stoma", "kök basıncı", "kohezyon", "soymuk"],
    "sinir-sistemi": ["nöron", "sinaps", "akson", "nörotransmitter", "miyelin", "uyartı", "refleks"],
    "endokrin-sistem": ["hormon", "hipotalamus", "hipofiz", "insülin", "adrenalin", "tiroksin", "diyabet",
                        "steroit"],
    "dolasim-sistemi": ["alyuvar", "akyuvar", "trombosit", "kılcal", "hemoglobin", "kalp"],
    "solunum-sistemi": ["alveol", "diyafram", "solunum merkezi", "hemoglobin", "bronş"],
    "sindirim-sistemi": ["pepsin", "amilaz", "safra", "villus", "peristaltik", "hidroklorik"],
    "bosaltim-sistemi": ["nefron", "amonyak", "idrar", "aldosteron", "diyaliz", "glomerulus"],
    "bagisiklik-sistemi": ["antijen", "fagositoz", "iltihap", "bellek hücre", "aktif bağışıklık"],
}

# "X öncesinde" ve "X'den önce" (bölünmeden önce) kalıpları önkoşulu gösterir; "ilk önce" gibi
# sıra belirten "önce" ise değildir.
CUE_BEFORE = re.compile(r"(?:(?<!\w)öncesi\w*|(?:meden|madan|den|dan|ten|tan)\s+önce)(?!\w)")
CUE_SUPPORT = re.compile(r"destek|uygula|kullan|sağla|etki|yardım|gerekli")
CUE_USE = re.compile(r"kullan|uygula|görev|önemli|sağla|üreme|büyüme|iyileş")
# Tür sıralaması: onkosul en güçlü, paragraf düzeyi en zayıf.
STRENGTH = {"onkosul": 4, "destek": 3, "ortak": 2}


def norm(text):
    # Türkçe büyük/küçük harf: İ->i, I->ı. Karakter sayısı değişmez, böylece
    # eşleşme konumları özgün metinde de geçerlidir.
    out = []
    for ch in text:
        if ch == "İ":
            out.append("i")
        elif ch == "I":
            out.append("ı")
        else:
            out.append(ch.lower())
    return "".join(out)


def phrase_re(phrase):
    return re.compile(r"(?<!\w)" + re.escape(norm(phrase)))


def split_sentences(paragraph):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip()]


def load_topics():
    topics = json.loads((KB / "corpus" / "topics.json").read_text(encoding="utf-8"))
    for t in topics:
        raw = (KB / t["passage"]).read_text(encoding="utf-8")
        body, _, rest = raw.partition("## Kaynaklar")
        t["paras"] = [p.strip() for p in body.split("\n\n") if p.strip() and not p.strip().startswith("#")]
        t["text"] = "\n".join(t["paras"])
        t["sources"] = []
        for line in rest.splitlines():
            m = re.match(r"\s*-\s+(.+?)\s+—\s+(.+)$", line)
            if m:
                url = next((u for prefix, u in SOURCE_LINKS.items() if m.group(1).startswith(prefix)), None)
                t["sources"].append({"ad": m.group(1), "tur": m.group(2), "url": url, "verified": url is not None})
    return topics


def terms_of(t):
    return ALIASES.get(t["id"], []) + [t["title"]]


def edge_terms_of(t):
    # "dna" ve "rna" sorgu için nukleik-asitlere yönlendirir, ama her DNA cümlesi
    # ayrı bir ilişki kanıtı değildir; kenar eşleşmesinde kullanılmaz.
    terms = terms_of(t) + EDGE_TERMS.get(t["id"], [])
    return [a for a in terms if a not in ("dna", "rna")]


def pick_center(query, topics):
    q = norm(query).strip()
    if not q:
        return None, None
    scored = []
    for t in topics:
        if q in (norm(t["title"]), t["id"], t["id"].replace("-", " ")):
            scored.append((100, t))
        elif any(phrase_re(a).search(q) for a in terms_of(t)):
            scored.append((80, t))
    if scored:
        scored.sort(key=lambda x: -x[0])
        return scored[0][1], None
    # Konu adı ya da eş anlamlısı değil: metinde en çok geçen konu merkez olur.
    counts = []
    for t in topics:
        n = len(phrase_re(q).findall(norm(t["text"])))
        if n:
            counts.append((n, t))
    if not counts:
        return None, None
    counts.sort(key=lambda x: -x[0])
    best = counts[0][1]
    note = f"'{query}' bir konu adı ya da eş anlamlısı değil; metinde en çok geçtiği konu ({best['title']}) merkez seçildi."
    return best, note


def direction_of(sent_ns, c_pos, t_pos, center_id, other_id):
    """'X öncesinde Y' kalıbında (source, target); kalıp ya da iki yön yoksa None."""
    cue = CUE_BEFORE.search(sent_ns)
    if not cue:
        return None
    # "X öncesinde Y": X sonra gelir (hedef), Y önce gelir (kaynak).
    if any(p < cue.start() for p in c_pos) and any(p > cue.start() for p in t_pos):
        return other_id, center_id
    if any(p < cue.start() for p in t_pos) and any(p > cue.start() for p in c_pos):
        return center_id, other_id
    return None


def candidates_between(center, other):
    """İki konu arasındaki cümle (level sentence) ve paragraf (level paragraph) adayları."""
    c_res = [phrase_re(a) for a in edge_terms_of(center)]
    t_res = [phrase_re(a) for a in edge_terms_of(other)]
    out = []
    for t in (center, other):
        # Paragraf düzeyinde kanıt, pasajın öbür konuyu anan cümlesidir.
        ext = t_res if t is center else c_res
        for para in t["paras"]:
            para_ns = norm(para)
            if not (any(r.search(para_ns) for r in c_res) and any(r.search(para_ns) for r in t_res)):
                continue
            sentence_hit = False
            for sent in split_sentences(para):
                ns = norm(sent)
                c_pos = [m.start() for r in c_res for m in r.finditer(ns)]
                t_pos = [m.start() for r in t_res for m in r.finditer(ns)]
                if not (c_pos and t_pos):
                    continue
                sentence_hit = True
                d = direction_of(ns, c_pos, t_pos, center["id"], other["id"])
                if d is not None:
                    kind, (source, target) = "onkosul", d
                elif CUE_SUPPORT.search(ns):
                    kind, source, target = "destek", center["id"], other["id"]
                else:
                    kind, source, target = "ortak", center["id"], other["id"]
                out.append({"type": kind, "source": source, "target": target, "level": "sentence",
                            "passage": t["passage"], "quote": sent})
            if not sentence_hit:
                quote = next((s for s in split_sentences(para) if any(r.search(norm(s)) for r in ext)), para)
                out.append({"type": "ortak", "source": center["id"], "target": other["id"],
                            "level": "paragraph", "passage": t["passage"], "quote": quote})
    return out


def pick_relation(cands):
    """Bir konu çifti için tek tür, yön ve kanıt kümesi seçer."""
    for kind in ("onkosul", "destek", "ortak"):
        same = [c for c in cands if c["type"] == kind and c["level"] == "sentence"]
        if same:
            break
    else:
        kind = "ortak"
        same = [c for c in cands if c["type"] == "ortak"]
    directions = Counter((c["source"], c["target"]) for c in same)
    source, target = directions.most_common(1)[0][0]
    chosen = [c for c in same if (c["source"], c["target"]) == (source, target)] if kind == "onkosul" else same
    conflict = kind == "onkosul" and len(directions) > 1
    seen, evidence = set(), []
    for c in chosen:
        if c["quote"] not in seen:
            seen.add(c["quote"])
            evidence.append({"passage": c["passage"], "quote": c["quote"]})
    grounded = any(c["level"] == "sentence" for c in chosen) and not conflict
    return {"source": source, "target": target, "type": kind, "evidence": evidence[:3],
            "status": "verified" if grounded else "inference", "conflict": conflict}


def build_edges(center, topics):
    edges = {}
    for other in topics:
        if other is center:
            continue
        cands = candidates_between(center, other)
        if cands:
            edges[other["id"]] = pick_relation(cands)
    return edges


def node_for(t, order, why):
    sents = split_sentences(t["paras"][0]) if t["paras"] else []
    uses = [s for s in split_sentences(t["text"]) if CUE_USE.search(norm(s))][:3]
    sources = sorted(t["sources"], key=lambda s: 0 if "MEB" in s["ad"] else 1)
    return {"id": t["id"], "title": t["title"], "grades": t["grades"],
            "summary": " ".join(sents[:2]), "uses": uses,
            "study": {"order": order, "why": why, "sources": sources}}


def build(query, topics):
    by_id = {t["id"]: t for t in topics}
    center, note = pick_center(query, topics)
    if center is None:
        return {"query": query, "center": None, "nodes": [], "edges": [],
                "unknowns": [f"'{query}' için bir konu bulunamadı: konu adı, başlığı ya da eş anlamlısı değil "
                             "ve pasajlarda geçmiyor."]}

    unknowns = []
    if note:
        unknowns.append(note)
    if center.get("grade_note"):
        unknowns.append(f"Sınıf notu (doğrulanmadı): {center['grade_note']}")

    edges = build_edges(center, topics)
    ranked = sorted(edges, key=lambda k: (-STRENGTH[edges[k]["type"]], edges[k]["status"] != "verified",
                                          -len(edges[k]["evidence"])))[:MAX_NEIGHBOURS]
    edges = {k: edges[k] for k in ranked}

    prereqs = [k for k in edges if edges[k]["type"] == "onkosul" and edges[k]["source"] == k]
    center_order = 2 if prereqs else 1
    center_why = "Aranan konunun kendisi."
    if prereqs:
        center_why += " Önce şu konulara bakılmalı: " + ", ".join(by_id[k]["title"] for k in prereqs) + "."

    nodes = [node_for(center, center_order, center_why)]
    for k in ranked:
        e = edges[k]
        t = by_id[k]
        if e["type"] == "onkosul" and e["source"] == k:
            order, why = 1, f"{center['title']} konusunu anlamak için önce bu konu gerekir."
        elif e["type"] == "onkosul":
            order, why = 3, f"{center['title']} öğrenildikten sonra çalışılır."
        else:
            order, why = 3, "İlişkili konu; aranan konuyla birlikte çalışılabilir."
        nodes.append(node_for(t, order, why))

    if not edges:
        unknowns.append("Bu konu için pasajlarda kanıtlı bir ilişki bulunamadı.")
    else:
        n_inf = sum(1 for e in edges.values() if e["status"] != "verified")
        n_conf = sum(1 for e in edges.values() if e["conflict"])
        unknowns.append(f"{len(edges)} ilişkiden {n_inf} tanesi çıkarım (kanıt cümlesi konuyu anıyor, ama ilişkinin "
                        "kendisi aynen yazılmamış). Tür ve yön cümle kalıbından çıkarıldı; yön yalnızca "
                        "'öncesinde' kalıbıyla verildi.")
        if n_conf:
            unknowns.append(f"{n_conf} ilişkide pasajlar birbiriyle çelişen yön veriyor; bunlar çıkarım sayıldı.")
    unknowns.append("Kaynak bağlantıları doğrulanmadı (url boş, verified false).")

    return {"query": query, "center": center["id"], "nodes": nodes,
            "edges": [{"source": e["source"], "target": e["target"], "type": e["type"],
                       "evidence": e["evidence"], "status": e["status"]} for e in edges.values()],
            "unknowns": unknowns}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    query = " ".join(sys.argv[1:]).strip()
    topics = load_topics()
    print(json.dumps(build(query, topics), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
