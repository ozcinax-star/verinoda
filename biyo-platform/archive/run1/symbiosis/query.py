"""Topic graph for one search: python query.py "<arama metni>"  ->  one JSON object on stdout (SPEC.md).

Symbiosis variant: the relations are read from the shared corpus with the `verinoda` package at runtime.

  * verinoda.textnorm      Turkish folding and suffix chains: "fotosentezle", "mitoza", "DNA'sını" are matched to
                           the topic they name.
  * verinoda.search_index  ranked retrieval over the Verinoda index of the corpus mirror (kb/): resolves a search
                           text that names no topic ("oksijen") and proposes topics that only share words with the
                           centre (those edges are "inference", never "verified").
  * verinoda.evidence      every quote becomes a file:line evidence record on the real corpus file and is re-read
                           with check_source; an edge is "verified" only when that check passes and the quote is a
                           literal substring of the cited line.

Without the verinoda package nothing is verified: every edge comes back as "inference" and `unknowns` says why.
stdout carries the JSON object only; anything Verinoda prints goes to stderr.
"""

from __future__ import annotations

import contextlib
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLATFORM = HERE.parent                      # biyo-platform/: corpus paths in the output are relative to it
CORPUS = PLATFORM / "corpus"
KB = HERE / "kb"                            # Verinoda project: a read-only mirror of corpus/ plus .verinoda/
DEFAULT_VERINODA_PYTHON = Path(r"C:\Users\ozcin\verinoda-mod\.venv\Scripts\python.exe")
REEXEC_FLAG = "BIYO_SYMBIOSIS_REEXEC"

MAX_NEIGHBOURS = 10
MAX_INFERENCE = 2
MIN_EDGE_WEIGHT = 0.7       # one mention by a strong term, or several weaker ones (MIN_EDGE_SUM)
MIN_EDGE_SUM = 1.2

# -- Verinoda: imported in this interpreter, or this script re-run with the interpreter that has it -----------

try:
    with contextlib.redirect_stdout(sys.stderr):
        from verinoda import evidence as v_evidence
        from verinoda import index as v_index
        from verinoda import search_index as v_search
        from verinoda.textnorm import fold_tr, is_suffix_chain
    VERINODA = True
except ImportError:
    VERINODA = False


def _reexec_with_verinoda() -> int | None:
    """Run this script again with a Python that imports verinoda; None when there is none."""
    if os.environ.get(REEXEC_FLAG):
        return None
    py = Path(os.environ.get("VERINODA_PYTHON") or DEFAULT_VERINODA_PYTHON)
    if not py.is_file() or py.resolve() == Path(sys.executable).resolve():
        return None
    env = dict(os.environ, **{REEXEC_FLAG: "1"})
    proc = subprocess.run([str(py), str(Path(__file__).resolve()), *sys.argv[1:]], env=env,
                          stdout=subprocess.PIPE)
    sys.stdout.buffer.write(proc.stdout)
    sys.stdout.flush()
    return proc.returncode


if not VERINODA:
    _FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ç": "c", "ç": "c", "Ğ": "g",
                           "ğ": "g", "Ö": "o", "ö": "o", "Ü": "u", "ü": "u", "Â": "a", "â": "a", "Î": "i",
                           "î": "i", "Û": "u", "û": "u"})
    _SUFFIXES = sorted("lar ler i ı u ü si in un nin nun e a ye ya de da te ta den dan ten tan le la yle yla "
                       "ndeki ndaki deki daki ki dir dur tir tur nde nda nden ndan ne na".split(), key=len,
                       reverse=True)

    def fold_tr(text: str) -> str:
        return text.translate(_FOLD).lower()

    def is_suffix_chain(rest: str, max_parts: int = 4) -> bool:
        if rest == "":
            return True
        if max_parts <= 0:
            return False
        return any(rest.startswith(s) and is_suffix_chain(rest[len(s):], max_parts - 1) for s in _SUFFIXES)


# -- the topic lexicon: how each topic is named in the corpus prose ------------------------------------------
# Folded word stems; a corpus word matches a stem when the rest of the word is a run of Turkish suffixes. Weight 1.0:
# the word names the topic; lower: a concept that belongs to the topic but is also used loosely elsewhere. Written
# from the passages (checked with `verinoda query`, see DEVLOG.md); PCR is left out on purpose, it belongs to both
# dna-replikasyonu and genetik-muhendisligi.

LEXICON: dict[str, list[tuple[str, float]]] = {
    "canlilarin-ortak-ozellikleri": [("canlilarin ortak ozellik", 1.0), ("ortak ozellik", 0.8),
                                     ("homeostazi", 0.6), ("metabolizma", 0.5)],
    "karbonhidratlar": [("karbonhidrat", 1.0), ("monosakkarit", 0.9), ("disakkarit", 0.9),
                        ("polisakkarit", 0.9), ("nisasta", 0.7), ("glikojen", 0.7), ("seluloz", 0.6),
                        ("sukroz", 0.6), ("glikoz", 0.5)],
    "proteinler": [("protein", 1.0), ("amino asit", 0.7), ("peptit bag", 0.7), ("denaturasyon", 0.6)],
    "lipitler": [("lipit", 1.0), ("fosfolipit", 0.9), ("yag asid", 0.8), ("yag asit", 0.8), ("steroit", 0.8),
                 ("kolesterol", 0.6), ("yag", 0.5)],
    "nukleik-asitler": [("nukleik asit", 1.0), ("nukleoti", 0.9), ("dna", 0.6), ("rna", 0.6),
                        ("mrna", 0.5), ("trna", 0.5)],
    "hucre-organelleri": [("organel", 1.0), ("hucre teorisi", 1.0), ("mitokondri", 0.7), ("kloroplast", 0.7),
                          ("ribozom", 0.7), ("golgi", 0.7), ("lizozom", 0.7), ("sentrozom", 0.7),
                          ("endoplazmik retikulum", 0.7), ("prokaryot", 0.5), ("okaryot", 0.4)],
    "hucre-zari-madde-gecisi": [("hucre zari", 1.0), ("aktif tasima", 1.0), ("pasif tasima", 1.0),
                                ("secici gecirgen", 0.8), ("endositoz", 0.9), ("ekzositoz", 0.9),
                                ("sodyum potasyum pompa", 0.8), ("tasiyici protein", 0.7), ("fagositoz", 0.6)],
    "difuzyon-ve-osmoz": [("difuzyon", 1.0), ("ozmoz", 1.0), ("osmoz", 1.0), ("plazmoliz", 0.9),
                          ("turgor", 0.8), ("hipotonik", 0.8), ("hipertonik", 0.8)],
    "enzimler": [("enzim", 1.0), ("katalizor", 0.9), ("aktivasyon enerji", 0.9), ("substrat", 0.8),
                 ("polimeraz", 0.6), ("helikaz", 0.6), ("ligaz", 0.6), ("amilaz", 0.6), ("lipaz", 0.6),
                 ("pepsin", 0.6), ("tripsin", 0.6)],
    "atp-enerji": [("atp", 1.0), ("adp", 0.8), ("fosforilasyon", 0.8), ("fotofosforilasyon", 0.8)],
    "fotosentez": [("fotosentez", 1.0), ("calvin dongu", 0.9), ("klorofil", 0.6)],
    "hucresel-solunum": [("hucresel solunum", 1.0), ("oksijenli solunum", 1.0), ("oksijensiz solunum", 1.0),
                         ("fermantasyon", 0.9), ("glikoliz", 0.9), ("krebs dongu", 0.9)],
    "mitoz": [("mitoz", 1.0), ("sitokinez", 0.8), ("hucre dongu", 0.7), ("hucre bolunme", 0.5)],
    "mayoz": [("mayoz", 1.0), ("krossing over", 0.9), ("gamet", 0.5)],
    "mendel-kalitimi": [("mendel", 1.0), ("kalitim", 0.9), ("alel", 0.6), ("genotip", 0.7), ("fenotip", 0.7),
                        ("homozigot", 0.7), ("heterozigot", 0.7), ("cekinik", 0.6)],
    "dna-replikasyonu": [("dna eslen", 1.0), ("eslenme", 1.0), ("eslenir", 1.0), ("eslenmez", 1.0),
                         ("replikasyon", 1.0), ("esler", 0.9)],
    "protein-sentezi": [("protein sentez", 1.0), ("transkripsiyon", 1.0), ("translasyon", 1.0),
                        ("kodon", 0.8), ("genetik sifre", 0.7)],
    "mutasyon": [("mutasyon", 1.0), ("mutajen", 0.9)],
    "genetik-muhendisligi": [("genetik muhendislig", 1.0), ("biyoteknoloji", 0.9), ("rekombinant", 0.9),
                             ("gen aktarim", 0.9), ("gdo", 0.8), ("gen tedavi", 0.8), ("crispr", 0.8)],
    "evrim": [("evrim", 1.0), ("dogal secilim", 1.0), ("yapay secilim", 0.9), ("adaptasyon", 0.7),
              ("darwin", 0.8), ("modifikasyon", 0.5)],
    "ekosistem": [("ekosistem", 1.0), ("besin zincir", 0.9), ("besin ag", 0.9), ("uretici", 0.7),
                  ("ayristirici", 0.7), ("tuketici", 0.6), ("komunite", 0.6), ("ekoloji", 0.6)],
    "madde-dongusu": [("madde dongu", 1.0), ("karbon dongu", 1.0), ("azot dongu", 1.0),
                      ("donguler halinde", 0.8), ("azot baglayici", 0.8), ("nitrifikasyon", 0.8)],
    "populasyon-ekolojisi": [("populasyon", 1.0), ("tasima kapasite", 0.9), ("nufus", 0.6)],
    "bitkilerde-su-tasinimi": [("odun boru", 1.0), ("soymuk boru", 1.0), ("ksilem", 1.0), ("floem", 1.0),
                               ("terleme", 0.8), ("kok basinc", 0.9), ("kok tuy", 0.8), ("emici tuy", 0.8),
                               ("stoma", 0.7)],
    "sinir-sistemi": [("sinir sistem", 1.0), ("noron", 0.9), ("sinir hucre", 0.9), ("uyarti", 0.8),
                      ("sinaps", 0.8), ("sinir", 0.7), ("hipotalamus", 0.5), ("omurilik", 0.6)],
    "endokrin-sistem": [("endokrin", 1.0), ("hormon", 1.0), ("hipofiz", 0.8), ("ic salgi", 0.8),
                        ("insulin", 0.6), ("adh", 0.6), ("tiroit", 0.6), ("hipotalamus", 0.5)],
    "dolasim-sistemi": [("dolasim", 1.0), ("kalp", 0.8), ("kalb", 0.8), ("kan damar", 0.8),
                        ("kilcal damar", 0.6), ("alyuvar", 0.5), ("hemoglobin", 0.5)],
    "solunum-sistemi": [("solunum sistem", 1.0), ("akciger", 0.8), ("alveol", 0.8), ("soluk", 0.7),
                        ("brons", 0.7), ("bronscuk", 0.7), ("diyafram", 0.7)],
    "sindirim-sistemi": [("sindirim", 1.0), ("ince bagirsak", 0.8), ("mide", 0.6), ("hidroliz", 0.6),
                         ("safra", 0.6)],
    "bosaltim-sistemi": [("bosaltim", 1.0), ("bobrek", 0.9), ("nefron", 0.9), ("idrar", 0.8)],
    "bagisiklik-sistemi": [("bagisiklik", 1.0), ("antikor", 0.9), ("antijen", 0.9), ("lenfosit", 0.9),
                           ("akyuvar", 0.6), ("asi", 0.0)],
}
# "asi" (aşı, vaccine) is a search synonym only: as a stem it would match "aşırı".
SEARCH_ONLY = {"asi"}

# Single-word searches a student types that the corpus supports but that are too loose to be mention terms.
SYNONYMS: dict[str, str] = {
    "dna": "nukleik-asitler", "rna": "nukleik-asitler", "nukleotit": "nukleik-asitler",
    "kalitim": "mendel-kalitimi", "genetik": "mendel-kalitimi", "gen": "mendel-kalitimi",
    "hucre": "hucre-organelleri", "bolunme": "mitoz", "hucre bolunmesi": "mitoz",
    "sinir": "sinir-sistemi", "beyin": "sinir-sistemi", "hormon": "endokrin-sistem",
    "kan": "dolasim-sistemi", "kalp": "dolasim-sistemi", "bobrek": "bosaltim-sistemi",
    "seker": "karbonhidratlar", "yag": "lipitler", "enerji": "atp-enerji", "ekoloji": "ekosistem",
    "besin zinciri": "ekosistem", "nufus": "populasyon-ekolojisi", "asi": "bagisiklik-sistemi",
    "replikasyon": "dna-replikasyonu", "dna eslenmesi": "dna-replikasyonu", "eslenme": "dna-replikasyonu",
    "biyoteknoloji": "genetik-muhendisligi", "dogal secilim": "evrim", "solunum": "solunum-sistemi",
}
AMBIGUOUS = {"solunum": ["solunum-sistemi", "hucresel-solunum"], "genetik": ["mendel-kalitimi",
                                                                             "genetik-muhendisligi"]}

# Sentence cues for prerequisite direction (folded). "X öncesinde Y": Y comes first. "X'in ilkelerine dayanır",
# "X'in anlaşılmasıyla mümkün": X comes first.
BEFORE_CUE = re.compile(r"\boncesinde\w*")
FOUNDATION_CUE = re.compile(r"\bilkeler\w*|\bdayan(?:ir|arak)\b|\banlasilmasiyla\b|\btemelini\b")
USE_CUE = re.compile(r"kullanil|saglar|yol acar|tarim|\btip\b|tedavi|gunluk|ornek|onem|hayati|uretil|"
                     r"yararlan|korur|duzenler")
WORD_RX = re.compile(r"\w+", re.UNICODE)
SENTENCE_RX = re.compile(r"(?<=[.!?])\s+(?=[A-ZÇĞİÖŞÜ0-9\"(])")


def norm(text: str) -> str:
    """Folded, lower-case, diacritics dropped; same length as fold_tr output for Turkish text."""
    t = fold_tr(unicodedata.normalize("NFC", text))
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))


def _compile_lexicon() -> list[tuple[str, tuple[str, ...], float]]:
    terms = [(tid, tuple(stem.split()), w) for tid, lst in LEXICON.items() for stem, w in lst
             if stem not in SEARCH_ONLY]
    # longest phrase first, so "protein sentezi" is protein-sentezi and not proteinler
    return sorted(terms, key=lambda t: (-len(t[1]), -sum(map(len, t[1]))))


TERMS = _compile_lexicon()


def _word_matches(stem: str, word: str) -> bool:
    return word == stem or (word.startswith(stem) and is_suffix_chain(word[len(stem):]))


def mentions(text: str) -> list[dict]:
    """Topic mentions in ``text``: [{topic, weight, start, end, term}] with character offsets, no overlaps."""
    toks = [(m.start(), m.end(), norm(m.group())) for m in WORD_RX.finditer(text)]
    taken = [False] * len(toks)
    found = []
    for tid, stems, w in TERMS:
        n = len(stems)
        for i in range(len(toks) - n + 1):
            if any(taken[i:i + n]):
                continue
            if all(_word_matches(stems[k], toks[i + k][2]) for k in range(n)):
                for k in range(n):
                    taken[i + k] = True
                found.append({"topic": tid, "weight": w, "start": toks[i][0], "end": toks[i + n - 1][1],
                              "term": " ".join(stems)})
    return sorted(found, key=lambda m: m["start"])


# -- corpus ---------------------------------------------------------------------------------------------------

def load_topics() -> dict[str, dict]:
    topics = json.loads((CORPUS / "topics.json").read_text(encoding="utf-8"))
    return {t["id"]: t for t in topics}


def read_passage(topic: dict) -> dict:
    """Lines, body sentences with their line numbers, and the Kaynaklar list of one passage."""
    rel = topic["passage"]
    lines = (PLATFORM / rel).read_text(encoding="utf-8").splitlines()
    sentences, sources, paragraphs = [], [], []
    in_sources = False
    for no, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("## "):
            in_sources = norm(s).startswith("## kaynak")
            continue
        if not s or s.startswith("# "):
            continue
        if in_sources:
            if s.startswith("- "):
                ad, _, tur = s[2:].rpartition(" — ")
                sources.append({"ad": (ad or tur).strip(), "tur": tur.strip() if ad else "",
                                "url": None, "verified": False})
            continue
        paragraphs.append((no, s))
        for sent in SENTENCE_RX.split(s):
            if sent.strip():
                sentences.append({"line": no, "text": sent.strip()})
    return {"rel": rel, "lines": lines, "sentences": sentences, "sources": sources, "paragraphs": paragraphs}


def min_grade(topic: dict) -> int:
    return min(topic.get("grades") or [99])


def grade_label(topic: dict) -> str:
    return "-".join(str(g) for g in topic.get("grades") or [])


# -- the Verinoda side: index mirror, retrieval, evidence -----------------------------------------------------

class Verinoda:
    """The verinoda package around the kb/ mirror. Every call keeps stdout clean (prints go to stderr)."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.graph = None
        if not VERINODA:
            return
        with contextlib.redirect_stdout(sys.stderr):
            try:
                self._sync_mirror()
                self.graph = v_index.load(KB)
            except Exception as exc:  # noqa: BLE001 - a broken index must not break the search
                self.notes.append(f"Verinoda indeksi açılamadı ({type(exc).__name__}); ilişkiler doğrulanmadan "
                                  "gösteriliyor.")
                self.graph = None

    def _sync_mirror(self) -> None:
        """kb/corpus mirrors corpus/ byte for byte; a change is copied and the Verinoda index updated."""
        stale = []
        for src in [CORPUS / "topics.json", *sorted((CORPUS / "passages").glob("*.md"))]:
            dst = KB / src.relative_to(PLATFORM)
            if not dst.is_file() or not filecmp.cmp(src, dst, shallow=False):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                stale.append(src.name)
        fresh_index = (KB / ".verinoda" / "atlas.db").is_file()
        if stale or not fresh_index:
            cmd = [sys.executable, "-m", "verinoda", "update" if fresh_index else "setup", "."]
            if not fresh_index:
                cmd += ["--agents", "none", "--no-mcp"]
            subprocess.run(cmd, cwd=KB, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)

    @property
    def ok(self) -> bool:
        return self.graph is not None

    def rank(self, text: str, limit: int = 30) -> list[dict]:
        """Passages Verinoda ranks for ``text``: [{file, line, score}] (best passage line of each hit)."""
        if not self.ok:
            return []
        with contextlib.redirect_stdout(sys.stderr):
            rk = v_search.rank(self.graph, text, limit=limit)
        out = []
        for h in rk.hits:
            if not h.file.startswith("corpus/passages/"):
                continue
            line = h.passages[0][2] if h.passages else h.a
            out.append({"file": h.file, "line": int(line), "score": float(h.score)})
        return out

    def verify(self, rel: str, line: int, quote: str) -> tuple[bool, str]:
        """A file:line evidence record on the real corpus file, re-read by Verinoda; the quote must be in it."""
        if not self.ok:
            return False, "verinoda yok"
        with contextlib.redirect_stdout(sys.stderr):
            ev = v_evidence.source_evidence(PLATFORM, rel, line, line, commit=None, source_type="design_doc")
            if ev is None:
                return False, "kanıt kaydı oluşturulamadı"
            chk = v_evidence.check_source(PLATFORM, ev)
            text = v_evidence.read_lines(PLATFORM / rel, line, line) or ""
        if not chk.ok:
            return False, chk.reason
        if quote not in text:
            return False, "alıntı satırda yok"
        return True, f"{ev['locator']} {ev['content_hash'][:19]}"


# -- search resolution ----------------------------------------------------------------------------------------

def resolve(query: str, topics: dict[str, dict], vn: Verinoda, unknowns: list[str]) -> str | None:
    q = norm(query).strip().strip("?!.,;:")
    q_spaced = re.sub(r"[\s_\-]+", " ", q).strip()
    if not q_spaced:
        return None
    for tid, t in topics.items():
        if q_spaced in (tid.replace("-", " "), norm(t["title"])):
            return tid
    if q_spaced in AMBIGUOUS:
        first, *rest = AMBIGUOUS[q_spaced]
        unknowns.append(f"'{query}' birden fazla konuya karşılık geliyor: "
                        + ", ".join(topics[x]["title"] for x in AMBIGUOUS[q_spaced])
                        + f". '{topics[first]['title']}' merkeze alındı; diğerini adıyla arayabilirsin.")
        return first
    for syn, tid in SYNONYMS.items():
        words = q_spaced.split()
        stems = syn.split()
        if len(words) == len(stems) and all(_word_matches(s, w) for s, w in zip(stems, words)):
            return tid
    for tid, t in topics.items():  # a title prefix: "mitoz bölünmesi", "fotosentez nedir"
        nt = norm(t["title"])
        if len(q_spaced) >= 4 and (nt.startswith(q_spaced) or q_spaced.startswith(nt)):
            return tid
    found = mentions(query)
    if found:
        best = max(found, key=lambda m: (m["weight"], m["end"] - m["start"]))
        if best["weight"] >= 0.5:
            others = sorted({m["topic"] for m in found} - {best["topic"]})
            if others:
                unknowns.append("Aramada başka konular da geçiyor: "
                                + ", ".join(topics[x]["title"] for x in others) + ".")
            return best["topic"]
    # No topic named: ask Verinoda which passage the words occur in, and accept it only when a query word really
    # occurs there (Verinoda's own stem expansion also returns near words: "bilinmeyen" -> "bilinen").
    words = [w for w in q_spaced.split() if len(w) >= 4]
    by_file = {t["passage"]: tid for tid, t in topics.items()}
    for hit in vn.rank(query, limit=10):
        tid = by_file.get(hit["file"])
        if not tid or not words:
            continue
        body = [norm(w) for w in WORD_RX.findall((PLATFORM / hit["file"]).read_text(encoding="utf-8"))]
        if any(_word_matches(w, b) for w in words for b in body):
            unknowns.append(f"'{query}' bir konu adı değil; Verinoda araması bu kelimeyi en çok "
                            f"'{topics[tid]['title']}' metninde buldu ({hit['file']}:{hit['line']}). "
                            "Merkezdeki konu bu yüzden seçildi.")
            return tid
    return None


# -- relations ------------------------------------------------------------------------------------------------

def sentence_relations(host: str, sent: dict, topics: dict[str, dict]) -> list[dict]:
    """Relations one sentence of ``host``'s passage states: [{other, weight, type, source, target, cue}]."""
    ms = mentions(sent["text"])
    others = [m for m in ms if m["topic"] != host]
    if not others:
        return []
    folded = norm(sent["text"])
    prereq_of: dict[str, tuple[str, str]] = {}  # other -> (source, target) when a cue fixes the order
    for rx, kind in ((BEFORE_CUE, "before"), (FOUNDATION_CUE, "foundation")):
        for cue in rx.finditer(folded):
            before = {m["topic"] for m in ms if m["end"] <= cue.start()}
            after = {m["topic"] for m in ms if m["start"] >= cue.end()}
            if kind == "before":    # "X öncesinde Y": Y first; the passage's own topic is X when unnamed
                first = after - before
                then = (before | ({host} if host not in after else set())) - first
            else:                   # "X'in ilkelerine dayanır": X first; else the passage's topic is the base
                first = (before - {host}) or {host}
                then = (({host} | after) if first != {host} else after) - first
            for a in first:
                for b in then - {a}:
                    other = b if a == host else a
                    if host in (a, b) and other != host:
                        prereq_of[other] = (a, b)
    best: dict[str, dict] = {}
    for m in others:
        cur = best.get(m["topic"])
        if cur is None or m["weight"] > cur["weight"]:
            best[m["topic"]] = {"other": m["topic"], "weight": m["weight"], "term": m["term"]}
    out = []
    for other, rel in best.items():
        if other in prereq_of:
            src, tgt = prereq_of[other]
            rel.update(type="onkosul", source=src, target=tgt, cue=True)
        else:
            # No cue: the grade order decides, but only when it is clear (a 9th-grade basics topic, or two grades
            # apart); a topic one grade later that the passage only names is an application of it, "destek".
            gh, go = min_grade(topics[host]), min_grade(topics[other])
            lo, hi = (other, host) if go < gh else (host, other)
            if go != gh and (min(go, gh) == 9 or abs(go - gh) >= 2):
                rel.update(type="onkosul", source=lo, target=hi, cue=False)
            else:
                rel.update(type="destek", source=host, target=other, cue=False)
        out.append(rel)
    return out


def find_edges(center: str, topics: dict[str, dict], passages: dict[str, dict], vn: Verinoda,
               unknowns: list[str]) -> list[dict]:
    pairs: dict[str, list[dict]] = {}
    for host, p in passages.items():
        for sent in p["sentences"]:
            for rel in sentence_relations(host, sent, topics):
                other = rel["other"]
                if center not in (host, other):
                    continue
                key = other if host == center else host
                pairs.setdefault(key, []).append({**rel, "host": host, "line": sent["line"],
                                                  "quote": sent["text"]})
    edges = []
    for other, rels in pairs.items():
        weights = [r["weight"] for r in rels]
        if max(weights) < MIN_EDGE_WEIGHT and sum(weights) < MIN_EDGE_SUM:
            continue
        cued = [r for r in rels if r["type"] == "onkosul" and r["cue"]]
        hosts = {r["host"] for r in rels}
        if cued:
            directions = {(r["source"], r["target"]) for r in cued}
            if len(directions) == 1:
                etype, (src, tgt) = "onkosul", directions.pop()
            else:
                etype, src, tgt = "ortak", center, other
                unknowns.append(f"{topics[center]['title']} ile {topics[other]['title']} arasında iki yönlü "
                                "önkoşul ipucu var; yön belirlenemedi, 'ortak' gösterildi.")
        elif rels[0]["type"] == "onkosul":
            etype, src, tgt = "onkosul", rels[0]["source"], rels[0]["target"]
        elif len(hosts) == 2:   # both passages name each other at the same grade
            etype, src, tgt = "ortak", center, other
        else:
            etype, src, tgt = "destek", rels[0]["source"], rels[0]["target"]
        # strongest sentence of each passage, at most two per edge
        ev_rels = []
        for h in sorted(hosts, key=lambda h: h != center):
            ev_rels.append(max((r for r in rels if r["host"] == h), key=lambda r: (r["cue"], r["weight"])))
        evidence, verified = [], True
        for r in ev_rels:
            ok, check = vn.verify(passages[r["host"]]["rel"], r["line"], r["quote"])
            verified = verified and ok
            evidence.append({"passage": passages[r["host"]]["rel"], "quote": r["quote"], "line": r["line"],
                             "check": check})
        edges.append({"source": src, "target": tgt, "type": etype, "evidence": evidence,
                      "status": "verified" if verified and evidence else "inference",
                      "_strength": max(weights) + 0.25 * (len(rels) - 1) + (0.5 if cued else 0)})
    edges.sort(key=lambda e: -e["_strength"])
    if len(edges) > MAX_NEIGHBOURS:
        unknowns.append(f"{len(edges) - MAX_NEIGHBOURS} zayıf bağlantı daha bulundu, grafikte gösterilmedi.")
        edges = edges[:MAX_NEIGHBOURS]
    edges += inference_edges(center, topics, edges, vn)
    for e in edges:
        e.pop("_strength", None)
    return edges


def inference_edges(center: str, topics: dict[str, dict], edges: list[dict], vn: Verinoda) -> list[dict]:
    """Passages Verinoda ranks high for the centre's title that name no topic of it in a sentence: "inference"."""
    linked = {center} | {e["source"] for e in edges} | {e["target"] for e in edges}
    by_file = {t["passage"]: tid for tid, t in topics.items()}
    out = []
    for hit in vn.rank(topics[center]["title"], limit=15):
        tid = by_file.get(hit["file"])
        if not tid or tid in linked or hit["score"] < 0.8:
            continue
        out.append({"source": center, "target": tid, "type": "ortak", "evidence": [], "status": "inference",
                    "note": f"Verinoda araması '{topics[center]['title']}' kelimelerini {hit['file']}:"
                            f"{hit['line']} satırında buldu; iki konuyu bağlayan bir cümle bulunamadı."})
        linked.add(tid)
        if len(out) >= MAX_INFERENCE:
            break
    return out


# -- nodes ----------------------------------------------------------------------------------------------------

def summary_of(p: dict) -> str:
    first = [s["text"] for s in p["sentences"] if s["line"] == p["paragraphs"][0][0]] if p["paragraphs"] else []
    out = ""
    for s in first:
        if out and len(out) + len(s) > 320:
            break
        out = f"{out} {s}".strip()
    return out


def uses_of(p: dict, summary: str) -> list[str]:
    picked = [s["text"] for s in p["sentences"] if s["text"] not in summary and USE_CUE.search(norm(s["text"]))]
    if not picked and p["paragraphs"]:
        picked = [s["text"] for s in p["sentences"] if s["line"] == p["paragraphs"][-1][0]][:1]
    return picked[:3]


def study_plan(center: str, topics: dict[str, dict], edges: list[dict]) -> dict[str, dict]:
    """Order: what the centre needs first (earliest grade first), the centre, what goes with it, what builds on it."""
    before, beside, after = [], [], []
    for e in edges:
        if e["type"] == "onkosul" and e["target"] == center:
            before.append(e["source"])
        elif e["type"] == "onkosul" and e["source"] == center:
            after.append(e["target"])
        else:
            beside.append(e["target"] if e["source"] == center else e["source"])
    key = lambda t: (min_grade(topics[t]), topics[t]["title"])  # noqa: E731
    ct = topics[center]["title"]
    plan, order = {}, 1
    for tid in sorted(set(before), key=key):
        plan[tid] = {"order": order, "why": f"'{ct}' konusunun önkoşulu: önce bunu çalış "
                                            f"({grade_label(topics[tid])}. sınıf)."}
        order += 1
    plan[center] = {"order": order, "why": f"Aradığın konu ({grade_label(topics[center])}. sınıf). "
                                           + ("Önkoşulları bitirdikten sonra çalış." if before else
                                              "Bu grafikte önkoşulu yok; buradan başlayabilirsin.")}
    order += 1
    for tid in sorted(set(beside) - set(plan), key=key):
        plan[tid] = {"order": order, "why": f"'{ct}' ile birlikte çalışılabilir: ortak kavramlar içerir "
                                            f"({grade_label(topics[tid])}. sınıf)."}
        order += 1
    for tid in sorted(set(after) - set(plan), key=key):
        plan[tid] = {"order": order, "why": f"'{ct}' konusunu öğrendikten sonra çalış: bu konu onun üzerine "
                                            f"kurulur ({grade_label(topics[tid])}. sınıf)."}
        order += 1
    return plan


def sources_of(p: dict) -> list[dict]:
    def rank(s: dict) -> int:
        n = norm(s["ad"])
        return 0 if n.startswith("meb") else 1 if "eba" in n else 2
    return sorted(p["sources"], key=rank)


def node(tid: str, topics: dict[str, dict], passages: dict[str, dict], plan: dict[str, dict]) -> dict:
    t, p = topics[tid], passages[tid]
    summary = summary_of(p)
    out = {"id": tid, "title": t["title"], "grades": list(t["grades"]), "summary": summary,
           "uses": uses_of(p, summary),
           "study": {**plan[tid], "sources": sources_of(p)}}
    if t.get("grade_note"):
        out["grade_note"] = t["grade_note"]
    return out


# -- main -----------------------------------------------------------------------------------------------------

def build(query: str) -> dict:
    topics = load_topics()
    unknowns: list[str] = []
    vn = Verinoda()
    center = resolve(query, topics, vn, unknowns)
    if center is None:
        return {"query": query, "center": None, "nodes": [], "edges": [],
                "unknowns": [f"'{query}' için corpus'ta bir konu bulunamadı. Bir konu adı (ör. fotosentez, "
                             "mitoz, sinir sistemi) ya da ders kitabındaki bir terim dene."] + vn.notes}
    passages = {tid: read_passage(t) for tid, t in topics.items()}
    edges = find_edges(center, topics, passages, vn, unknowns)
    ids = [center] + [x for e in edges for x in (e["source"], e["target"]) if x != center]
    ids = list(dict.fromkeys(ids))
    plan = study_plan(center, topics, edges)
    nodes = [node(tid, topics, passages, plan) for tid in ids]
    if not VERINODA:
        unknowns.append("Verinoda paketi bulunamadı: alıntılar doğrulanamadı, bütün ilişkiler 'inference'.")
    unknowns += vn.notes
    if topics[center].get("grade_note"):
        unknowns.append(f"Sınıf bilgisi corpus/topics.json'dan alındı ({grade_label(topics[center])}); "
                        f"kaynak notu: {topics[center]['grade_note']}")
    if edges:
        unknowns.append("İlişki türü ve yönü (önkoşul/destek/ortak) cümledeki ipuçlarından ve sınıf sırasından "
                        "kuralla çıkarıldı; bir öğretmen kontrol etmedi. 'verified' yalnızca alıntının metinde "
                        "gerçekten geçtiğini söyler.")
    unknowns.append("Kaynakların bağlantıları (URL) doğrulanmadı.")
    return {"query": query, "center": center, "nodes": nodes, "edges": edges, "unknowns": unknowns}


def main() -> int:
    if not VERINODA:
        code = _reexec_with_verinoda()
        if code is not None:
            return code
    query = " ".join(sys.argv[1:]).strip()
    try:
        result = build(query)
    except Exception as exc:  # noqa: BLE001 - the contract: a JSON object, never a traceback on stdout
        result = {"query": query, "center": None, "nodes": [], "edges": [],
                  "unknowns": [f"Beklenmeyen hata: {type(exc).__name__}: {exc}"]}
    # ASCII-escaped JSON: the same object whatever code page the reading process decodes stdout with
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
