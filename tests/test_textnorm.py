"""textnorm: Turkish folding, apostrophe suffixes, stopwords, vocabulary-checked stemming."""

from __future__ import annotations

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


def test_tr_stem_candidates_longest_first():
    cands = tn.tr_stem_candidates("veritabanına")
    assert cands[0] == "veritabanina" and "veritabani" in cands
    assert all(len(a) >= len(b) for a, b in zip(cands, cands[1:]))
