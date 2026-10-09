"""Baseline relation finder for the biology learning platform.

    python query.py "<arama metni>"

Prints one JSON object (the SPEC output contract) to stdout and nothing else.

How relations are found (no ML, no external packages):
  1. Every topic has a hand-written list of Turkish term patterns (TERMS) that signal it in prose,
     e.g. "eşlen" / "replikasyon" for dna-replikasyonu.
  2. Each passage is split into sentences. A sentence in topic A's passage that matches a term of
     topic B is evidence for an A-B relation. The quote is the sentence itself, copied verbatim.
  3. The relation type comes from cue words in the evidence sentences (CUES), overridden by a small
     list of curriculum rules (CURRICULUM_RULES) for well-known prerequisite pairs.
  4. Direction of an `onkosul` edge comes from the curriculum rule if there is one, otherwise from a
     hand-assigned conceptual level per topic (molecules -> cell -> processes -> systems -> ecology).
  5. status: "verified" only when every evidence quote is re-found verbatim in its passage file;
     "inference" when an edge comes from a curriculum rule without a quote; "unknown" otherwise.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))
# The corpus is shared and read-only; it lives next to this variant folder.
CORPUS_ROOT = os.path.normpath(os.path.join(HERE, ".."))

MAX_NEIGHBOURS = 10
MIN_SCORE = 3          # weighted mentions needed before a co-mention counts as a relation
MAX_QUOTES_PER_EDGE = 3
MIN_BRANCHES = 4       # below this, weaker quoted co-mentions (score 2) are admitted too

# --------------------------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------------------------

_ASCII_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def tr_lower(text: str) -> str:
    """Turkish-aware lowercase (I -> ı, İ -> i)."""
    return text.replace("I", "ı").replace("İ", "i").lower()


def fold(text: str) -> str:
    """Lowercase + strip Turkish diacritics + collapse punctuation; used for search matching."""
    text = tr_lower(text).translate(_ASCII_FOLD)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÇĞİÖŞÜ0-9(\"'])")


def split_sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(paragraph) if s.strip()]


# --------------------------------------------------------------------------------------------
# Hand-written knowledge (this is the "model" of the baseline; all of it is visible here)
# --------------------------------------------------------------------------------------------

# Term patterns per topic, as regex fragments over tr_lower() text. A word-start boundary is added
# automatically; Turkish suffixes are allowed after the fragment. weight: 3 = the topic's own name,
# 2 = a defining concept, 1 = a weaker hint.
TERMS: dict[str, list[tuple[str, int]]] = {
    "canlilarin-ortak-ozellikleri": [(r"canlıların ortak özellik", 3), (r"homeostazi", 2),
                                     (r"metabolizma", 1), (r"canlılığ", 1)],
    "karbonhidratlar": [(r"karbonhidrat", 3), (r"monosakkarit", 2), (r"polisakkarit", 2), (r"disakkarit", 2),
                        (r"nişasta", 2), (r"glikojen", 2), (r"glikoz", 1), (r"selüloz", 1), (r"riboz", 1),
                        (r"deoksiriboz", 1)],
    "proteinler": [(r"protein(?!\s+sentez)", 3), (r"amino asit", 2), (r"peptit bağ", 2), (r"denatürasyon", 1),
                   (r"hemoglobin", 1), (r"antikor", 1)],
    "lipitler": [(r"lipit", 3), (r"fosfolipit", 3), (r"yağ asid", 2), (r"yağ asit", 2), (r"yağlar", 2),
                 (r"steroit", 2), (r"kolesterol", 2), (r"gliserol", 1)],
    "nukleik-asitler": [(r"nükleik asit", 3), (r"nükleotit", 2), (r"rna\b", 2), (r"mrna", 2), (r"trna", 2),
                        (r"çift sarmal", 2), (r"baz dizi", 2), (r"dna\b", 1), (r"dna'", 1)],
    "hucre-organelleri": [(r"organel", 3), (r"mitokondri", 2), (r"kloroplast", 2), (r"ribozom", 2),
                          (r"golgi", 2), (r"lizozom", 2), (r"endoplazmik", 2), (r"sentrozom", 2),
                          (r"hücre teori", 3), (r"çekirdek zar", 1), (r"ökaryot", 1), (r"prokaryot", 1)],
    "hucre-zari-madde-gecisi": [(r"hücre zar", 3), (r"aktif taşıma", 2), (r"pasif taşıma", 2),
                                (r"seçici geçirgen", 2), (r"endositoz", 2), (r"ekzositoz", 2),
                                (r"fagositoz", 2), (r"taşıyıcı protein", 2), (r"sodyum-potasyum pompa", 2)],
    "difuzyon-ve-osmoz": [(r"difüzyon", 3), (r"ozmoz", 3), (r"hipotonik", 2), (r"hipertonik", 2),
                          (r"plazmoliz", 2), (r"turgor", 2)],
    "enzimler": [(r"enzim", 3), (r"katalizör", 2), (r"polimeraz", 1), (r"amilaz", 1), (r"lipaz", 1),
                 (r"pepsin", 1), (r"helikaz", 1), (r"ligaz", 1)],
    "atp-enerji": [(r"atp\b", 3), (r"atp'", 3), (r"adenozin trifosfat", 3), (r"fosforilasyon", 2)],
    "fotosentez": [(r"fotosentez", 3), (r"klorofil", 2), (r"calvin", 2), (r"üreticiler", 1)],
    "hucresel-solunum": [(r"hücresel solunum", 3), (r"oksijenli solunum", 3), (r"oksijensiz solunum", 3),
                         (r"fermantasyon", 2), (r"glikoliz", 2), (r"krebs", 2)],
    "mitoz": [(r"mitoz", 3), (r"hücre döngüsü", 2), (r"sitokinez", 2), (r"hücre bölünme", 1),
              (r"eşeysiz üreme", 1)],
    "mayoz": [(r"mayoz", 3), (r"krossing over", 2), (r"gamet", 2), (r"homolog kromozom", 2),
              (r"döllenme", 1), (r"üreme hücre", 1)],
    "mendel-kalitimi": [(r"mendel", 3), (r"alel", 2), (r"genotip", 2), (r"fenotip", 2), (r"baskın", 1),
                        (r"çekinik", 1), (r"kalıtım\b", 1), (r"kalıtımın", 1)],
    "dna-replikasyonu": [(r"replikasyon", 3), (r"eşlen", 3), (r"dna'sını eşler", 3), (r"kendini eşler", 3),
                         (r"pcr\b", 2), (r"dna polimeraz", 2)],
    "protein-sentezi": [(r"protein sentez", 3), (r"transkripsiyon", 2), (r"translasyon", 2), (r"kodon", 2),
                        (r"genetik şifre", 2)],
    "mutasyon": [(r"mutasyon", 3), (r"mutajen", 2)],
    "genetik-muhendisligi": [(r"genetik mühendisliğ", 3), (r"rekombinant", 2), (r"gen aktarım", 2),
                             (r"pcr\b", 2), (r"gdo\b", 2), (r"biyoteknoloji", 2), (r"gen tedavi", 2)],
    "evrim": [(r"evrim", 3), (r"doğal seçilim", 3), (r"yapay seçilim", 2), (r"adaptasyon", 2),
              (r"ortak ata", 2), (r"genetik çeşitlilik", 1), (r"kalıtsal çeşitlilik", 1)],
    "ekosistem": [(r"ekosistem", 3), (r"besin zincir", 2), (r"besin ağı", 2), (r"komünite", 2),
                  (r"ayrıştırıcı", 2), (r"üreticiler", 1), (r"ekoloji", 1)],
    "madde-dongusu": [(r"madde döngü", 3), (r"karbon döngü", 3), (r"azot döngü", 3), (r"döngüler hâlinde", 2),
                      (r"azot bağlayıcı", 2), (r"nitrat", 1)],
    "populasyon-ekolojisi": [(r"popülasyon", 3), (r"taşıma kapasitesi", 2), (r"nüfus", 1)],
    "bitkilerde-su-tasinimi": [(r"odun boru", 3), (r"soymuk boru", 3), (r"ksilem", 3), (r"floem", 3),
                               (r"terleme", 2), (r"kök tüy", 2), (r"emici tüy", 2), (r"kök basınc", 2),
                               (r"bitkilerin kökleri", 2), (r"topraktan", 1)],
    "sinir-sistemi": [(r"sinir sistem", 3), (r"nöron", 2), (r"sinir hücre", 2), (r"uyartı", 2), (r"sinaps", 2),
                      (r"hipotalamus", 1), (r"omurilik", 1), (r"sinir bağlantı", 1)],
    "endokrin-sistem": [(r"endokrin", 3), (r"hormon", 3), (r"insülin", 2), (r"hipofiz", 2), (r"adh\b", 2),
                        (r"aldosteron", 2), (r"iç salgı", 2)],
    "dolasim-sistemi": [(r"dolaşım", 3), (r"kılcal", 2), (r"kal[pb]", 2), (r"alyuvar", 2), (r"atardamar", 2),
                        (r"toplardamar", 2), (r"kan damar", 2), (r"plazma\b", 1), (r"lenf", 1)],
    "solunum-sistemi": [(r"solunum sistem", 3), (r"akciğer", 3), (r"alveol", 2), (r"soluk al", 2),
                        (r"soluk ver", 2)],
    "sindirim-sistemi": [(r"sindirim sistem", 3), (r"sindirim kanal", 3), (r"ince bağırsak", 2),
                         (r"mide", 2), (r"sindirim", 1), (r"sindiril", 1), (r"hidroliz", 1)],
    "bosaltim-sistemi": [(r"boşaltım", 3), (r"böbrek(?!üstü)", 3), (r"nefron", 2), (r"idrar", 2),
                         (r"üre(?:ye|yi|nin)?\b", 1)],
    "bagisiklik-sistemi": [(r"bağışıklık", 3), (r"antikor", 2), (r"antijen", 2), (r"lenfosit", 2),
                           (r"akyuvar", 2)],
}

# Phrases whose match must not count for a topic (false friends found while tuning).
EXCLUDE: dict[str, list[str]] = {
    "sindirim-sistemi": [r"hücre içi sindirim", r"sindirim enzimleriyle hücre içi"],
    "canlilarin-ortak-ozellikleri": [r"metabolizma hızını"],
    "mitoz": [r"mitokondri"],
}

# Conceptual level: lower = more foundational. Used only to orient `onkosul` edges that have
# no explicit curriculum rule. Ties fall back to the earliest grade.
LEVEL: dict[str, int] = {
    "canlilarin-ortak-ozellikleri": 0,
    "karbonhidratlar": 1, "lipitler": 1, "proteinler": 1, "nukleik-asitler": 1,
    "hucre-organelleri": 2, "enzimler": 2, "atp-enerji": 2,
    "hucre-zari-madde-gecisi": 3, "difuzyon-ve-osmoz": 3,
    "fotosentez": 4, "hucresel-solunum": 4, "dna-replikasyonu": 4, "protein-sentezi": 4,
    "mitoz": 5, "mayoz": 5, "mutasyon": 5,
    "mendel-kalitimi": 6, "genetik-muhendisligi": 6,
    "sinir-sistemi": 6, "endokrin-sistem": 6, "dolasim-sistemi": 6, "solunum-sistemi": 6,
    "sindirim-sistemi": 6, "bosaltim-sistemi": 6, "bagisiklik-sistemi": 6, "bitkilerde-su-tasinimi": 6,
    "ekosistem": 7, "madde-dongusu": 7, "populasyon-ekolojisi": 7, "evrim": 7,
}

# Cue words that suggest a dependency (onkosul) or an application/support (destek) in a sentence.
CUES = {
    "onkosul": [r"öncesinde", r"\bönce\b", r"dayanır", r"ilkelerin", r"mümkün olmuş", r"anlaşılmasıyla",
                r"yapı birim", r"yapısına katıl", r"karşılığı", r"açıklaması", r"hammadde", r"ham madde",
                r"ürünüdür", r"ürünleridir", r"ürünü\b", r"kaynağıdır", r"temel kaynak", r"bu sayede",
                r"gerçekleştiği için", r"yol açabilir", r"oluşur\b"],
    "destek": [r"kullan", r"sağla", r"taşı", r"yararlan", r"düzenle", r"rol oyna", r"görev", r"etkile",
               r"uygulama", r"aktar", r"üretir", r"örne", r"korur"],
}

# Curriculum knowledge written by hand: (source, target, type). It fixes type and direction for
# pairs a biology teacher would agree on. An edge that exists only here (no sentence found) is
# reported as "inference", never "verified".
CURRICULUM_RULES: list[tuple[str, str, str]] = [
    ("proteinler", "enzimler", "onkosul"),
    ("nukleik-asitler", "dna-replikasyonu", "onkosul"),
    ("nukleik-asitler", "protein-sentezi", "onkosul"),
    ("proteinler", "protein-sentezi", "onkosul"),
    ("dna-replikasyonu", "mitoz", "onkosul"),
    ("dna-replikasyonu", "mayoz", "onkosul"),
    ("mitoz", "mayoz", "onkosul"),
    ("mayoz", "mendel-kalitimi", "onkosul"),
    ("dna-replikasyonu", "mutasyon", "onkosul"),
    ("protein-sentezi", "mutasyon", "onkosul"),
    ("mutasyon", "evrim", "onkosul"),
    ("mayoz", "evrim", "onkosul"),
    ("protein-sentezi", "genetik-muhendisligi", "onkosul"),
    ("dna-replikasyonu", "genetik-muhendisligi", "onkosul"),
    ("atp-enerji", "fotosentez", "onkosul"),
    ("atp-enerji", "hucresel-solunum", "onkosul"),
    ("karbonhidratlar", "hucresel-solunum", "onkosul"),
    ("enzimler", "sindirim-sistemi", "onkosul"),
    ("lipitler", "hucre-zari-madde-gecisi", "onkosul"),
    ("hucre-zari-madde-gecisi", "difuzyon-ve-osmoz", "ortak"),
    ("difuzyon-ve-osmoz", "solunum-sistemi", "onkosul"),
    ("difuzyon-ve-osmoz", "bitkilerde-su-tasinimi", "onkosul"),
    ("fotosentez", "hucresel-solunum", "destek"),
    ("fotosentez", "ekosistem", "onkosul"),
    ("ekosistem", "madde-dongusu", "onkosul"),
    ("ekosistem", "populasyon-ekolojisi", "onkosul"),
    ("sinir-sistemi", "endokrin-sistem", "destek"),
    ("dolasim-sistemi", "bagisiklik-sistemi", "destek"),
    ("hucresel-solunum", "solunum-sistemi", "destek"),
    ("mendel-kalitimi", "evrim", "onkosul"),
    ("mendel-kalitimi", "mutasyon", "ortak"),
]

# Search synonyms (folded form). The first id is the chosen centre; the others are offered as
# alternatives in `unknowns`.
SYNONYMS: dict[str, list[str]] = {
    "solunum": ["solunum-sistemi", "hucresel-solunum"],
    "hucre": ["hucre-organelleri", "hucre-zari-madde-gecisi"],
    "hucre bolunmesi": ["mitoz", "mayoz"],
    "bolunme": ["mitoz", "mayoz"],
    "genetik": ["mendel-kalitimi", "genetik-muhendisligi", "mutasyon"],
    "kalitim": ["mendel-kalitimi"], "kalitsal": ["mendel-kalitimi"], "mendel": ["mendel-kalitimi"],
    "gen": ["mendel-kalitimi"], "alel": ["mendel-kalitimi"], "genotip": ["mendel-kalitimi"],
    "fenotip": ["mendel-kalitimi"], "kan grubu": ["mendel-kalitimi"], "bezelye": ["mendel-kalitimi"],
    "fotosentez": ["fotosentez"], "klorofil": ["fotosentez"], "calvin": ["fotosentez"],
    "mitoz": ["mitoz"], "hucre dongusu": ["mitoz"], "sitokinez": ["mitoz"],
    "mayoz": ["mayoz"], "gamet": ["mayoz"], "krossing over": ["mayoz"], "eseyli ureme": ["mayoz"],
    "sinir": ["sinir-sistemi"], "noron": ["sinir-sistemi"], "beyin": ["sinir-sistemi"],
    "sinaps": ["sinir-sistemi"], "refleks": ["sinir-sistemi"], "omurilik": ["sinir-sistemi"],
    "dna": ["nukleik-asitler", "dna-replikasyonu"], "rna": ["nukleik-asitler"],
    "nukleotit": ["nukleik-asitler"], "nukleik asit": ["nukleik-asitler"],
    "replikasyon": ["dna-replikasyonu"], "eslenme": ["dna-replikasyonu"], "dna eslenmesi": ["dna-replikasyonu"],
    "protein": ["proteinler", "protein-sentezi"], "amino asit": ["proteinler"],
    "protein sentezi": ["protein-sentezi"], "transkripsiyon": ["protein-sentezi"],
    "translasyon": ["protein-sentezi"], "kodon": ["protein-sentezi"], "genetik sifre": ["protein-sentezi"],
    "atp": ["atp-enerji"], "enerji": ["atp-enerji"], "hucrede enerji": ["atp-enerji"],
    "organel": ["hucre-organelleri"], "mitokondri": ["hucre-organelleri"], "ribozom": ["hucre-organelleri"],
    "kloroplast": ["hucre-organelleri", "fotosentez"], "golgi": ["hucre-organelleri"],
    "hucre zari": ["hucre-zari-madde-gecisi"], "madde gecisi": ["hucre-zari-madde-gecisi"],
    "aktif tasima": ["hucre-zari-madde-gecisi"], "pasif tasima": ["hucre-zari-madde-gecisi"],
    "endositoz": ["hucre-zari-madde-gecisi"], "ekzositoz": ["hucre-zari-madde-gecisi"],
    "difuzyon": ["difuzyon-ve-osmoz"], "ozmoz": ["difuzyon-ve-osmoz"], "osmoz": ["difuzyon-ve-osmoz"],
    "plazmoliz": ["difuzyon-ve-osmoz"], "turgor": ["difuzyon-ve-osmoz"],
    "enzim": ["enzimler"], "katalizor": ["enzimler"],
    "karbonhidrat": ["karbonhidratlar"], "glikoz": ["karbonhidratlar"], "nisasta": ["karbonhidratlar"],
    "seker": ["karbonhidratlar"],
    "lipit": ["lipitler"], "yag": ["lipitler"], "yaglar": ["lipitler"], "kolesterol": ["lipitler"],
    "mutasyon": ["mutasyon"], "mutajen": ["mutasyon"],
    "genetik muhendisligi": ["genetik-muhendisligi"], "biyoteknoloji": ["genetik-muhendisligi"],
    "gdo": ["genetik-muhendisligi"], "pcr": ["genetik-muhendisligi", "dna-replikasyonu"],
    "crispr": ["genetik-muhendisligi"], "gen aktarimi": ["genetik-muhendisligi"],
    "evrim": ["evrim"], "dogal secilim": ["evrim"], "darwin": ["evrim"], "adaptasyon": ["evrim"],
    "ekosistem": ["ekosistem"], "ekoloji": ["ekosistem", "populasyon-ekolojisi"],
    "besin zinciri": ["ekosistem"], "besin agi": ["ekosistem"],
    "madde dongusu": ["madde-dongusu"], "karbon dongusu": ["madde-dongusu"], "azot dongusu": ["madde-dongusu"],
    "populasyon": ["populasyon-ekolojisi"], "nufus": ["populasyon-ekolojisi"],
    "bitkilerde tasima": ["bitkilerde-su-tasinimi"], "ksilem": ["bitkilerde-su-tasinimi"],
    "floem": ["bitkilerde-su-tasinimi"], "odun borusu": ["bitkilerde-su-tasinimi"],
    "soymuk borusu": ["bitkilerde-su-tasinimi"], "terleme": ["bitkilerde-su-tasinimi"],
    "hormon": ["endokrin-sistem"], "endokrin": ["endokrin-sistem"], "insulin": ["endokrin-sistem"],
    "hipofiz": ["endokrin-sistem"], "tiroit": ["endokrin-sistem"],
    "dolasim": ["dolasim-sistemi"], "kalp": ["dolasim-sistemi"], "kan": ["dolasim-sistemi"],
    "damar": ["dolasim-sistemi"],
    "akciger": ["solunum-sistemi"], "alveol": ["solunum-sistemi"],
    "hucresel solunum": ["hucresel-solunum"], "fermantasyon": ["hucresel-solunum"],
    "glikoliz": ["hucresel-solunum"], "krebs": ["hucresel-solunum"],
    "sindirim": ["sindirim-sistemi"], "mide": ["sindirim-sistemi"], "bagirsak": ["sindirim-sistemi"],
    "bosaltim": ["bosaltim-sistemi"], "bobrek": ["bosaltim-sistemi"], "idrar": ["bosaltim-sistemi"],
    "nefron": ["bosaltim-sistemi"],
    "bagisiklik": ["bagisiklik-sistemi"], "antikor": ["bagisiklik-sistemi"], "antijen": ["bagisiklik-sistemi"],
    "asi": ["bagisiklik-sistemi"], "lenfosit": ["bagisiklik-sistemi"],
    "canli": ["canlilarin-ortak-ozellikleri"], "canlilik": ["canlilarin-ortak-ozellikleri"],
    "homeostazi": ["canlilarin-ortak-ozellikleri"], "metabolizma": ["canlilarin-ortak-ozellikleri"],
}

TYPE_TR = {"onkosul": "ön koşul", "destek": "destek / uygulama", "ortak": "ortak kavram"}

# --------------------------------------------------------------------------------------------
# Data provider. This is the one place that knows where content comes from; an EBA-backed
# provider would replace load_corpus() and keep its return shape.
# --------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_corpus() -> dict:
    with open(os.path.join(CORPUS_ROOT, "corpus", "topics.json"), encoding="utf-8") as fh:
        topics = json.load(fh)
    by_id = {}
    for t in topics:
        rel = t["passage"]
        with open(os.path.join(CORPUS_ROOT, *rel.split("/")), encoding="utf-8") as fh:
            raw = fh.read()
        by_id[t["id"]] = {**t, "raw": raw, **parse_passage(raw)}
    return {"order": [t["id"] for t in topics], "topics": by_id}


def parse_passage(raw: str) -> dict:
    body, _, src = raw.partition("## Kaynaklar")
    paragraphs = []
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if block and not block.startswith("#"):
            paragraphs.append(block)
    sentences = [s for p in paragraphs for s in split_sentences(p)]
    sources = []
    for line in src.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        ad, sep, tur = line[2:].rpartition(" — ")
        if not sep:
            ad, tur = line[2:], ""
        sources.append({"ad": ad.strip(), "tur": tur.strip()})
    return {"paragraphs": paragraphs, "sentences": sentences, "sources": sources}


# --------------------------------------------------------------------------------------------
# Relation extraction
# --------------------------------------------------------------------------------------------

_B = r"(?<![a-zçğıöşüâîû0-9])"


@lru_cache(maxsize=1)
def compiled_terms() -> dict[str, list[tuple[re.Pattern, int]]]:
    return {tid: [(re.compile(_B + frag), w) for frag, w in terms] for tid, terms in TERMS.items()}


def mention_weight(sentence_lc: str, topic_id: str) -> int:
    """Highest term weight of topic_id found in a lowercased sentence (0 if none)."""
    text = sentence_lc
    for ex in EXCLUDE.get(topic_id, []):
        text = re.sub(ex, " ", text)
    best = 0
    for pat, w in compiled_terms()[topic_id]:
        if w > best and pat.search(text):
            best = w
    return best


def sentence_type(sentence_lc: str) -> str:
    for kind in ("onkosul", "destek"):
        if any(re.search(c, sentence_lc) for c in CUES[kind]):
            return kind
    return "ortak"


def relation_index(center: str) -> dict[frozenset, dict]:
    """Co-mention records for every pair that involves `center`, keyed by unordered pair.

    Two directions are scanned: the centre's passage for other topics' terms, and every other
    passage for the centre's terms."""
    corpus = load_corpus()
    ids = corpus["order"]
    pairs: dict[frozenset, dict] = {}

    def add(passage_owner: str, other: str, sent: str, lc: str, w: int):
        rec = pairs.setdefault(frozenset((center, other)), {"score": 0, "quotes": [], "types": []})
        rec["score"] += w
        rec["quotes"].append({"passage": corpus["topics"][passage_owner]["passage"], "quote": sent,
                              "weight": w, "from": passage_owner})
        rec["types"].append(sentence_type(lc))

    for sent in corpus["topics"][center]["sentences"]:
        lc = tr_lower(sent)
        for b in ids:
            if b != center:
                w = mention_weight(lc, b)
                if w:
                    add(center, b, sent, lc, w)
    for b in ids:
        if b == center:
            continue
        for sent in corpus["topics"][b]["sentences"]:
            lc = tr_lower(sent)
            w = mention_weight(lc, center)
            if w:
                add(b, b, sent, lc, w)
    for rec in pairs.values():
        # Mentioned from both passages: stronger signal.
        if len({q["from"] for q in rec["quotes"]}) == 2:
            rec["score"] += 2
    return pairs


def quote_is_in_corpus(passage_rel: str, quote: str) -> bool:
    try:
        with open(os.path.join(CORPUS_ROOT, *passage_rel.split("/")), encoding="utf-8") as fh:
            return quote in fh.read()
    except OSError:
        return False


def orient(a: str, b: str, topics: dict) -> tuple[str, str, bool]:
    """(source, target, decided) for an onkosul edge using level, then earliest grade."""
    la, lb = LEVEL.get(a, 9), LEVEL.get(b, 9)
    if la != lb:
        return (a, b, True) if la < lb else (b, a, True)
    ga, gb = min(topics[a]["grades"]), min(topics[b]["grades"])
    if ga != gb:
        return (a, b, True) if ga < gb else (b, a, True)
    return (a, b, False) if a < b else (b, a, False)


def pick_quotes(quotes: list[dict]) -> list[dict]:
    """Strongest quotes first, at least one from each side when both sides mention each other."""
    ranked = sorted(quotes, key=lambda q: -q["weight"])
    chosen, seen_from = [], set()
    for q in ranked:
        if q["from"] not in seen_from:
            chosen.append(q)
            seen_from.add(q["from"])
    for q in ranked:
        if len(chosen) >= MAX_QUOTES_PER_EDGE:
            break
        if q not in chosen:
            chosen.append(q)
    return [{"passage": q["passage"], "quote": q["quote"]} for q in chosen[:MAX_QUOTES_PER_EDGE]]


def edges_for(center: str, unknowns: list[str]) -> list[dict]:
    corpus = load_corpus()
    topics = corpus["topics"]
    index = relation_index(center)
    rules = {frozenset((s, t)): (s, t, k) for s, t, k in CURRICULUM_RULES}

    candidates, weak = [], []
    for other in corpus["order"]:
        if other == center:
            continue
        key = frozenset((center, other))
        rec = index.get(key)
        rule = rules.get(key)
        if rec is None and rule is None:
            continue
        score = rec["score"] if rec else 0
        entry = (score + (3 if rule else 0), other, rec, rule)
        if rec is not None and rule is None and score < MIN_SCORE:
            weak.append(entry)
            continue
        candidates.append(entry)
    # A topic the corpus rarely links to anything would show an almost empty graph; let weaker
    # (still quoted) co-mentions in until it has a few branches.
    weak.sort(key=lambda c: -c[0])
    while len(candidates) < MIN_BRANCHES and weak and weak[0][0] >= 2:
        candidates.append(weak.pop(0))
    candidates.sort(key=lambda c: (-c[0], corpus["order"].index(c[1])))
    dropped = candidates[MAX_NEIGHBOURS:]
    if dropped:
        unknowns.append(
            "Daha zayıf bağlantılar gösterilmedi: " + ", ".join(topics[c[1]]["title"] for c in dropped) + "."
        )

    edges = []
    for score, other, rec, rule in candidates[:MAX_NEIGHBOURS]:
        evidence = pick_quotes(rec["quotes"]) if rec else []
        if rule:
            source, target, kind = rule
        else:
            types = rec["types"]
            kind = max(("onkosul", "destek", "ortak"), key=lambda k: (types.count(k), k == "onkosul"))
            source, target = center, other
            if kind == "onkosul":
                source, target, decided = orient(center, other, topics)
                if not decided:
                    unknowns.append(
                        f"'{topics[center]['title']}' ile '{topics[other]['title']}' arasındaki ön koşul "
                        "yönü belirlenemedi; yön varsayıldı."
                    )
            elif kind == "ortak":
                source, target = sorted((center, other))
        if evidence:
            status = "verified" if all(quote_is_in_corpus(e["passage"], e["quote"]) for e in evidence) else "unknown"
        else:
            status = "inference"
        edges.append({
            "source": source,
            "target": target,
            "type": kind,
            "evidence": evidence,
            "status": status,
            "basis": "müfredat kuralı + metin" if (rule and evidence) else ("müfredat kuralı" if rule else "metin"),
        })
    return edges


# --------------------------------------------------------------------------------------------
# Node content
# --------------------------------------------------------------------------------------------

USE_CUES = [r"kullanıl", r"sağlanır", r"sağlar", r"örne", r"günlük", r"tıp", r"tarım", r"hastal",
            r"tedavi", r"yol açar", r"önem", r"kaynağıdır", r"yapımında", r"hayati", r"dayanır",
            r"yoludur"]


def summarize(topic: dict) -> str:
    first = topic["sentences"][:2]
    text = " ".join(first)
    return text if len(text) <= 360 else first[0]


def uses_for(topic_id: str, edges: list[dict]) -> list[str]:
    topics = load_corpus()["topics"]
    topic = topics[topic_id]
    out = []
    for s in topic["sentences"][1:]:
        if any(re.search(c, tr_lower(s)) for c in USE_CUES):
            out.append(s)
        if len(out) >= 3:
            break
    builds_on, alongside = [], []
    for e in edges:
        if topic_id not in (e["source"], e["target"]):
            continue
        other = e["target"] if e["source"] == topic_id else e["source"]
        if e["type"] == "onkosul" and e["source"] == topic_id:
            builds_on.append(topics[other]["title"])
        elif e["type"] == "destek":
            alongside.append(topics[other]["title"])
    if builds_on:
        out.append("Temel oluşturduğu konular: " + ", ".join(dict.fromkeys(builds_on)) + ".")
    if alongside:
        out.append("Birlikte kullanıldığı konular: " + ", ".join(dict.fromkeys(alongside)) + ".")
    return out


def sources_for(topic: dict) -> list[dict]:
    def prefix(src):
        ad, tur = src["ad"], src["tur"]
        if ad.startswith("MEB") and not tur.startswith("MEB"):
            tur = "MEB " + tur
        elif ad.startswith("EBA") and not tur.startswith("EBA"):
            tur = "EBA " + tur
        return tur

    def rank(src):
        ad = src["ad"]
        if ad.startswith("MEB") and "ders kitabı" in ad:
            return 0
        if ad.startswith("MEB"):
            return 1
        if ad.startswith("EBA"):
            return 2
        return 3

    # No source was checked against a live catalogue, so none is marked verified and none has a URL.
    return [{"ad": s["ad"], "tur": prefix(s), "url": None, "verified": False}
            for s in sorted(topic["sources"], key=rank)]


def grades_text(grades: list[int]) -> str:
    return "-".join(str(g) for g in grades)


def study_plan(center: str, node_ids: list[str], edges: list[dict]) -> dict[str, dict]:
    topics = load_corpus()["topics"]
    ct = topics[center]["title"]
    role = {center: "center"}
    for e in edges:
        other = e["target"] if e["source"] == center else e["source"]
        if e["type"] == "onkosul":
            role[other] = "before" if e["target"] == center else "after"
        else:
            role[other] = "with"
    bucket = {"before": 0, "center": 1, "with": 2, "after": 3}

    def key(t):
        return (bucket[role.get(t, "with")], LEVEL.get(t, 9), min(topics[t]["grades"]), t)

    plan = {}
    for i, tid in enumerate(sorted(node_ids, key=key), start=1):
        t = topics[tid]
        g = grades_text(t["grades"])
        r = role.get(tid, "with")
        if r == "center":
            why = f"Aradığın konu. MEB programında {g}. sınıf konusu olarak geçer."
        elif r == "before":
            why = f"'{ct}' konusundan ÖNCE çalış: onu anlamak için gereken temel. ({g}. sınıf)"
        elif r == "after":
            why = f"'{ct}' konusundan SONRA çalış: bu konu onun üzerine kurulur. ({g}. sınıf)"
        else:
            why = f"'{ct}' ile birlikte tekrar et: ortak kavramları var ya da birbirini destekler. ({g}. sınıf)"
        if t.get("grade_note"):
            why += " Not: sınıf bilgisi tartışmalı, ayrıntı için konu notuna bak."
        plan[tid] = {"order": i, "why": why, "sources": sources_for(t)}
    return plan


# --------------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def search_table() -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for tid, t in load_corpus()["topics"].items():
        for key in (fold(tid), fold(t["title"])):
            table.setdefault(key, [])
            if tid not in table[key]:
                table[key].insert(0, tid)
        # Title without parenthesis, e.g. "Nükleik asitler (DNA ve RNA)" -> "nukleik asitler".
        short = fold(t["title"].split("(")[0])
        table.setdefault(short, [])
        if tid not in table[short]:
            table[short].insert(0, tid)
    for syn, ids in SYNONYMS.items():
        bucket = table.setdefault(fold(syn), [])
        for tid in ids:
            if tid not in bucket:
                bucket.append(tid)
    return table


def resolve(query: str) -> tuple[list[str], str]:
    """Return (candidate ids, how matched). The first id is the centre."""
    q = fold(query)
    if not q:
        return [], "empty"
    table = search_table()
    if q in table:
        return table[q], "exact"
    padded = f" {q} "
    # A known phrase inside a longer query ("fotosentez nedir").
    hits = [k for k in table if f" {k} " in padded]
    if hits:
        return table[max(hits, key=len)], "phrase"
    # A suffixed word ("kalıtımın", "mitozu"): a known key of 4+ letters starts a query word.
    words = q.split()
    stems = [k for k in table if len(k) >= 4 and " " not in k and any(w.startswith(k) for w in words)]
    if stems:
        return table[max(stems, key=len)], "stem"
    # A truncated word ("fotosen"): a query word of 5+ letters starts a known key.
    prefixes = [k for k in table if any(len(w) >= 5 and k.startswith(w) for w in words)]
    if prefixes:
        return table[min(prefixes, key=len)], "prefix"
    # A typo ("fotosentz").
    close = difflib.get_close_matches(q, list(table), n=1, cutoff=0.82)
    if close:
        return table[close[0]], "typo"
    return [], "none"


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def build_graph(query: str) -> dict:
    corpus = load_corpus()
    topics = corpus["topics"]
    unknowns: list[str] = []
    candidates, how = resolve(query or "")
    if not candidates:
        unknowns.append(
            f"'{query}' için bu derlemde bir konu bulunamadı. Konu adını farklı yazmayı dene "
            "(örnek: fotosentez, mitoz, sinir sistemi, kalıtım)."
        )
        return {"query": query, "center": None, "nodes": [], "edges": [], "unknowns": unknowns}

    center = candidates[0]
    if len(candidates) > 1:
        unknowns.append(
            f"'{query}' birden fazla konuya uyuyor; '{topics[center]['title']}' seçildi. Diğerleri: "
            + ", ".join(topics[c]["title"] for c in candidates[1:]) + "."
        )
    if how == "typo":
        unknowns.append(f"'{query}' tam eşleşmedi; en yakın konu '{topics[center]['title']}' gösteriliyor.")

    edges = edges_for(center, unknowns)
    node_ids = [center] + [e["target"] if e["source"] == center else e["source"] for e in edges]
    plan = study_plan(center, node_ids, edges)

    nodes = []
    for tid in node_ids:
        t = topics[tid]
        node = {
            "id": tid,
            "title": t["title"],
            "grades": list(t["grades"]),
            "summary": summarize(t),
            "uses": uses_for(tid, edges),
            "study": plan[tid],
        }
        if t.get("grade_note"):
            node["grade_note"] = t["grade_note"]
        nodes.append(node)

    if topics[center].get("grade_note"):
        unknowns.append(f"Sınıf bilgisi tartışmalı ({t_grades(topics[center])}): {topics[center]['grade_note']}")
    if any(e["status"] == "inference" for e in edges):
        unknowns.append("Bazı bağlantılar metinde alıntıyla bulunamadı; müfredat bilgisine dayanan çıkarımdır.")
    return {"query": query, "center": center, "nodes": nodes, "edges": edges, "unknowns": unknowns}


def t_grades(t: dict) -> str:
    return grades_text(t["grades"]) + ". sınıf"


def main(argv: list[str]) -> int:
    query = " ".join(argv[1:]).strip()
    try:
        result = build_graph(query)
    except Exception as exc:  # never crash the caller; report instead
        result = {"query": query, "center": None, "nodes": [], "edges": [],
                  "unknowns": [f"İç hata: {type(exc).__name__}: {exc}"]}
    # ASCII-escaped JSON is valid regardless of the console code page on Windows.
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
