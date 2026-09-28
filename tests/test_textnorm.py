"""textnorm: Turkish folding, apostrophe suffixes, stopwords, vocabulary-checked stemming."""

from __future__ import annotations

import pytest

from verinoda import textnorm as tn


def test_fold_is_length_preserving_and_keeps_dotted_i():
    for s in ("İndirim", "ÇAĞIRIYOR", "veritabanına", "Sipariş", "ışık"):
        assert len(tn.fold_tr(s)) == len(s)
    assert tn.fold_tr("İndirim") == "indirim"
    assert tn.fold_tr("çağırıyor") == "cagiriyor"


def test_apostrophe_suffix_is_split_off():
    assert tn.split_apostrophe("pricing.py'deki") == ("pricing.py", "deki")
    assert tn.split_apostrophe("compute_total’ı") == ("compute_total", "ı")
    assert tn.split_apostrophe("plain") == ("plain", "")


def test_words_drop_turkish_question_words_and_suffix_fragments_but_keep_db():
    ws = tn.words("İndirim nerede uygulanıyor ve pricing.py'deki hangi fonksiyon db'ye yazıyor?")
    assert "indirim" in ws and "db" in ws
    for junk in ("nerede", "hangi", "ve", "deki", "ye"):
        assert junk not in ws


def test_split_identifier():
    assert tn.split_identifier("getOrderRepo_v2") == ["get", "order", "repo", "v2"]
    assert tn.split_identifier("HTTPServer") == ["http", "server"]


def test_tr_stem_only_accepts_vocabulary_confirmed_stems():
    vocab = {"siparis", "order", "indirim", "veritabani"}
    assert tn.tr_stem("siparişin", vocab) == "siparis"
    assert tn.tr_stem("veritabanına", vocab) == "veritabani"
    # no confirmation -> 5-character prefix fallback
    assert tn.tr_stem("fiyatlandırmayı", set()) == "fiyat"


def test_has_turkish():
    assert tn.has_turkish("Sipariş nerede kaydediliyor?")
    assert tn.has_turkish("nasil ve nerede")
    assert not tn.has_turkish("Where is the order saved?")


# -- question-understanding helpers ------------------------------------------------------------

def test_en_stem_is_light_and_leaves_short_words():
    assert tn.en_stem("variables") == "variabl"
    assert tn.en_stem("orders") == "order"
    assert tn.en_stem("db") == "db"
    assert tn.en_stem("compute_total") == "compute_total"  # identifiers are not stemmed


def test_suffix_role_maps_turkish_cases():
    assert tn.suffix_role("den") == "source"
    assert tn.suffix_role("dan") == "source"
    assert tn.suffix_role("ye") == "target"
    assert tn.suffix_role("na") == "target"
    assert tn.suffix_role("deki") == "container"
    assert tn.suffix_role("in") == "owner"
    assert tn.suffix_role("yla") == "instrument"
    assert tn.suffix_role("xyz") is None


def test_is_suffix_chain_accepts_inflection_only():
    assert tn.is_suffix_chain("")
    assert tn.is_suffix_chain("iliyor")    # yaz+ıl+ıyor
    assert tn.is_suffix_chain("ar")        # yaz+ar
    assert tn.is_suffix_chain("ilim")      # also yaz+ıl+ım: callers let a longer key (yazılım) win first
    assert not tn.is_suffix_chain("gisayar")
    assert not tn.is_suffix_chain("xq")


def test_ground_key_unifies_case_apostrophes_and_turkish_letters():
    assert tn.ground_key("API’den") == tn.ground_key("api'DEN")
    assert tn.ground_key("SİPARİŞ") == tn.ground_key("sipariş") == "siparis"
    assert tn.ground_key("a  \n b") == "a b"


def test_detect_language():
    assert tn.detect_language("Sipariş nerede kaydediliyor?") == "tr"
    assert tn.detect_language("kayit nerede tutuluyor") == "tr"
    assert tn.detect_language("Where is the order saved?") == "en"
    assert tn.detect_language("Where is sipariş kaydediliyor?") == "mixed"
    assert tn.detect_language("") == "other"


def test_detect_language_follows_the_prose_of_an_english_issue():
    # "I've" split at the apostrophe is "ve", the Turkish "and": four of them made an English issue Turkish
    issue = ("I've also left a comment about the rest of the directives. I've fixed this bug, but I've moved the "
             "test to the validator and we've reverted the changes to the runner.")
    assert tn.detect_language(issue) == "en"
    # a Turkish word quoted in English prose, a name with Turkish letters, code in the message: still English
    assert tn.detect_language("Where is the ölçü field read?") == "en"
    assert tn.detect_language('The label shows "sürüm" instead of version when the locale is tr; where is it '
                              "chosen?") == "en"
    assert tn.detect_language("Gödel numbering: where is the encoder that the parser calls defined?") == "en"
    assert tn.detect_language("Where is the parser defined?\n```js\nvar ne = de(o, ki)\nvar bu = en\n```\n"
                              "Run it with `-o ve` and see https://example.org/o/ve/de") == "en"
    # Turkish stays Turkish: suffixes after an apostrophe count, code does not
    assert tn.detect_language("API'de sipariş nereden geliyor?") == "tr"
    assert tn.detect_language("Bu kod neden hata veriyor?\n```\nfor x in items:\n    if x is None: pass\n```") == "tr"
    assert tn.detect_language("`sipariş`") == "tr"
    # a Turkish question that quotes English stays Turkish (mixed)
    assert tn.detect_language('Uygulama açılırken "The system cannot find the file specified" hatası veriyor, bu '
                              "hata nereden geliyor?") == "mixed"
    assert tn.detect_language("Sipariş nerede kaydediliyor, the order service or the repository?") == "mixed"


@pytest.mark.parametrize("msg", [
    # a Turkish question about a pasted English error, issue body or explanation: the user asked in Turkish
    "Bu hata neden oluyor?\nERROR: could not open the file because it is locked by another process and the lock "
    "was not released in time",
    "Bu issue'yu nasıl çözerim?\n\nWhen I call compute_total with an empty list, it returns 0 but the docs say it "
    "should raise. I have checked the tests and they do not cover it. Is this a bug or is it expected?",
    "compute_total ne yapar? it is called by the service and by the tests",
    "Neden?\n> The quick fix is to set the flag and then restart the service when it is done",
    # ... or the Turkish question comes last, after the pasted English
    "When I call compute_total with an empty list, it returns 0 but the docs say it should raise. I have checked "
    "the tests and they do not cover it.\n\nBunu nasıl çözerim?",
    # a lone ``` in a sentence opens no code block: the Turkish question after it is still prose
    "The label is wrapped in ``` when the order is saved and it is shown to the user. Bu neden böyle oluyor, "
    "sipariş kaydı nerede yapılıyor, hangi fonksiyon çağırıyor?",
])
def test_a_turkish_question_quoting_english_stays_turkish(msg):
    assert tn.detect_language(msg) == "mixed"


def test_fences_and_lone_backticks():
    assert tn.detect_language("Bu neden oluyor?\n```\nthe file is locked by the other process and it is\n```") == "tr"
    # a fence opened after text, ending its line, still takes the block out of the prose
    assert tn.detect_language("Bu neden oluyor? ```python\nfor it in the_list: print(it)\n```") == "tr"
    # closed on one line
    assert tn.detect_language("Bu ```the one of the``` neden oluyor?") == "tr"


def test_clip_middle_keeps_the_head_and_the_last_words():
    assert tn.clip_middle("Which functions call apply_discount?", 60) == "Which functions call apply_discount?"
    out = tn.clip_middle("Which functions in the service layer call apply_discount?", 34)
    assert len(out) <= 34 and out.startswith("Which") and out.endswith(" … apply_discount?")
    out = tn.clip_middle("compute_total fonksiyonu hangi modülde ve hangi dosyada tanımlı?", 36)
    assert len(out) <= 36 and out.startswith("compute_total") and out.endswith("tanımlı?")
    assert "\n" not in tn.clip_middle("a\nb " * 40, 30) and len(tn.clip_middle("a\nb " * 40, 30)) <= 30


def test_clip_keeps_one_line_and_cuts_at_a_word():
    assert tn.clip("how is an order saved?", 300) == "how is an order saved?"
    assert tn.clip("a\r\n\r\nb   c", 300) == "a b c"
    long = "word " * 100
    out = tn.clip(long, 60)
    assert len(out) <= 60 and out.endswith("…") and set(out[:-1].split(" ")) == {"word"}
    assert tn.clip("x" * 100, 10) == "x" * 9 + "…"


def test_tr_stem_candidates_longest_first():
    cands = tn.tr_stem_candidates("veritabanına")
    assert cands[0] == "veritabanina" and "veritabani" in cands
    assert all(len(a) >= len(b) for a, b in zip(cands, cands[1:]))
