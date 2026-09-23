"""Repo-learned lexicon (docs/DESIGN.md D6): G2 associations with sites, filters, incremental
builds, the grounded TR->EN seed dictionary, and the graph-only fallback."""

from __future__ import annotations

import json

import networkx as nx
import pytest

from verinoda import lexicon
from verinoda.index import Graph

ORDERS = '''"""Sipariş işlemleri."""


def save_order(order):
    """Siparişi veritabanına kaydeder."""
    return order


def cancel_order(order):
    """Siparişi iptal eder."""
    return None


def load_order(order_id):
    """Siparişi yükler."""
    raise ValueError("Sipariş bulunamadı")


def list_orders():
    """Tüm siparişleri listeler."""
    return []


def price_order(order):
    # siparişin toplam tutarını hesaplar
    return 0


def ship_order(order):
    """Siparişi kargoya verir."""
    return order


def apply_discount(total):
    """İndirim uygular."""
    return total
'''
INVOICES = '''

def create_invoice(customer):
    """Fatura oluşturur."""
    return {}


def send_invoice(invoice):
    """Faturayı gönderir."""
    return True


def void_invoice(invoice):
    """Faturayı iptal eder."""
    return True


def print_invoice(invoice):
    """Faturayı yazdırır."""
    return ""


def archive_invoice(invoice):
    """Faturayı arşivler."""
    return None


def notify_customer(customer):
    """Müşteriye fatura bilgisini gönderir."""
    send_invoice(customer)
'''
MISC = '''

def parse_config(text):
    """Ayarları ayrıştırır."""
    return {}


def read_env(name):
    """Ortam değişkenini okur."""
    return None


def start_server(port):
    """Sunucuyu başlatır."""
    return None


def stop_server():
    """Sunucuyu durdurur."""
    return None


def clear_cache():
    """Önbelleği temizler."""
    return None


def log_event(event):
    """Olayı günlüğe yazar."""
    return None
'''
TESTS = '''
def test_save_order():
    """Sipariş fatura kargo indirim hepsi burada."""
    assert True
'''
README = "# Shop\n\nSipariş kaydı `save_order` ile yapılır ve fatura `create_invoice` ile oluşturulur.\n"


@pytest.fixture()
def shop(tmp_path):
    root = tmp_path / "shop_repo"
    for rel, text in {"shop/orders.py": ORDERS, "shop/invoices.py": INVOICES, "shop/misc.py": MISC,
                      "tests/test_orders.py": TESTS, "README.md": README}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    return root


def test_build_learns_turkish_words_to_identifier_parts_with_sites(shop):
    stats = lexicon.build(shop)
    assert stats["files"] == 5 and stats["reparsed"] == 5 and stats["units"] > 10
    lx = lexicon.load(shop)
    assert lx is not None and lx.source == "file"
    order = {p["part"]: p for p in lx.associations("siparişi")}
    assert "order" in order, lx.associations("siparişi")
    pr = order["order"]
    assert pr["g2"] >= lexicon.G2_MIN and pr["support"] >= lexicon.MIN_SUPPORT and 0 < pr["score"] <= 1
    assert pr["sites"] and all(s.startswith(("shop/orders.py:", "README.md:")) for s in pr["sites"])
    for site in pr["sites"]:  # every site points at a real line
        f, _, ln = site.rpartition(":")
        assert 1 <= int(ln) <= len((shop / f).read_text(encoding="utf-8").splitlines())
    invoice = {p["part"] for p in lx.associations("faturayı")}
    assert "invoice" in invoice
    # callee names are not parts: notify_customer() calls send_invoice() but is not "invoice"
    assert "notify" not in {p["part"] for p in lx.associations("siparişi")}


def test_filters_tests_stoplist_and_self_pairs(shop):
    lexicon.build(shop)
    lx = lexicon.load(shop)
    for word in ("sipariş", "fatura"):
        for pr in lx.associations(word):
            assert pr["part"] not in lexicon.PART_STOPLIST
            assert not any(s.startswith("tests/") for s in pr["sites"])  # test units never associate
    raw = json.loads(lexicon.lexicon_path(shop).read_text(encoding="utf-8"))
    assert all(p["part"] != w for w, prs in raw["pairs"].items() for p in prs)  # no word -> itself


def test_rebuild_is_incremental(shop):
    first = lexicon.build(shop)
    assert first["reparsed"] == 5
    again = lexicon.build(shop)
    assert again["reparsed"] == 0 and again["pairs"] == first["pairs"]
    p = shop / "shop" / "misc.py"
    p.write_bytes(p.read_bytes() + b"\n\ndef flush_queue():\n    \"\"\"Kuyrugu bosaltir.\"\"\"\n")
    assert lexicon.build(shop)["reparsed"] == 1
    # an explicit change list limits re-extraction to the files named
    q = shop / "shop" / "invoices.py"
    q.write_bytes(q.read_bytes() + b"\n# fatura notu\n")
    p.write_bytes(p.read_bytes() + b"\n# not listed\n")
    assert lexicon.build(shop, changed=[q])["reparsed"] == 1
    # file hashes given by the caller (a snapshot) spare re-hashing; a matching hash keeps the units
    raw = json.loads(lexicon.lexicon_path(shop).read_text(encoding="utf-8"))
    hashes = {f: v["sha256"] for f, v in raw["files"].items()}
    assert lexicon.build(shop, file_hashes=hashes, tree_hash="t1")["reparsed"] == 0
    assert lexicon.load(shop).tree_hash == "t1"


def test_lexicon_file_is_lf_utf8_json(shop):
    lexicon.build(shop)
    data = lexicon.lexicon_path(shop).read_bytes()
    assert b"\r" not in data
    raw = json.loads(data.decode("utf-8"))
    assert raw["version"] == lexicon.LEXICON_VERSION and raw["params"]["g2_min"] == lexicon.G2_MIN


def test_load_missing_or_foreign_version_is_none(shop):
    assert lexicon.load(shop) is None
    path = lexicon.lexicon_path(shop)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"version": 999}')
    assert lexicon.load(shop) is None
    path.write_bytes(b"not json")
    assert lexicon.load(shop) is None


def test_seed_dictionary_is_grounded_and_longest_key_wins(shop):
    lexicon.build(shop)
    lx = lexicon.load(shop)
    (hit,) = lx.seed(["indirimi"])
    assert hit["key"] == "indirim" and hit["targets"] == ["discount"]  # not indir -> download
    (hit,) = lx.seed(["siparisler"])
    assert hit["targets"] == ["order"]
    (hit,) = lx.seed(["police"])
    assert hit["targets"] == [] and hit["dropped"] == ["policy"]  # "policy" does not occur in this repository
    (hit,) = lx.seed(["kargoya"])
    assert hit["targets"] == ["shipment"]  # grounded through ship_order (shipment ~ ship), cargo dropped
    # a short key needs inflection after it: "yazılıyor" is yaz+ıl+ıyor, "yazılım" is its own entry
    assert lx.seed(["yaziliyor"])[0]["key"] == "yaz"
    assert lx.seed(["yazilim"])[0]["key"] == "yazilim"
    assert lx.seed(["yazgisayar"]) == []
    # multi-word keys match adjacent words
    two = lx.seed(["ortam", "degiskenleri"])
    assert two[0]["key"] == "ortam degisken" and two[0]["n"] == 2


def test_seed_keys_match_softened_final_consonants():
    """reddet -> reddediyor, istek -> isteği, grup -> grubu; only before a vowel-initial suffix."""
    keys = lambda w: [k for _, _, k, _ in lexicon.seed_lookup([w])]  # noqa: E731
    assert keys("reddediyor") == ["reddet"] and "refuse" in lexicon.seed_entries()["reddet"]
    assert keys("istegi") == ["istek"] and keys("grubu") == ["grup"] and keys("esigi") == ["esik"]
    assert keys("egitim") == [] and keys("reddedm") == []  # softened stem needs a vowel after it
    assert not lexicon.seed_key_matches("istek", "istegx")


def test_seed_covers_command_and_home_directory_words():
    """Found by running Verinoda on its own repo: "init ve scan komutları ev dizininde ... reddediyor mu?"."""
    from verinoda import textnorm as tn

    words = [tn.fold_tr(w) for w in "init ve scan komutları ev dizininde çalıştırılınca reddediyor mu".split()]
    found = {words[s]: (k, t) for s, _, k, t in lexicon.seed_lookup(words)}
    assert found["komutlari"][0] == "komut" and "command" in found["komutlari"][1]
    assert found["ev"] == ("ev dizin", ("home",))
    assert found["reddediyor"][0] == "reddet"


def test_seed_file_entries_are_folded_and_nonempty():
    entries = lexicon.seed_entries()
    assert len(entries) >= 200
    from verinoda.textnorm import fold_tr

    for key, targets in entries.items():
        assert key == " ".join(fold_tr(w) for w in key.split()) and targets
        assert all(t == t.lower() for t in targets)


def test_vocabulary_df_and_idf(shop):
    lexicon.build(shop)
    lx = lexicon.load(shop)
    assert lx.has("order") and lx.has("orders") and lx.has("invoice")
    assert lx.has("persist") is False
    assert lx.df("flux") == 0 and lx.idf("flux") > lx.idf("order")
    assert lx.grounded("discount") and not lx.grounded("trial_balance")


def test_text_hits_point_at_units(shop):
    lexicon.build(shop)
    lx = lexicon.load(shop)
    sites = lx.text_hits("bulunamadı")  # a user-visible message inside load_order
    assert any(s.startswith("shop/orders.py:") for s in sites)


def test_g2_is_zero_for_independence_and_positive_for_association():
    assert lexicon.g2(5, 10, 50, 100) == pytest.approx(0.0, abs=1e-9)
    assert lexicon.g2(6, 6, 6, 18) > lexicon.G2_MIN


def test_from_graph_vocabulary_grounds_seeds_without_a_file(tmp_path):
    G = nx.MultiDiGraph()
    G.add_node("a", label="apply_discount()", source_file="shop/pricing.py", file_type="code")
    G.add_node("b", label="pricing.py", source_file="shop/pricing.py", file_type="code")
    lx = lexicon.from_graph(Graph(G=G, path=tmp_path / "g.json", root=tmp_path))
    assert lx.source == "graph" and lx.has("discount") and lx.has("pricing")
    assert lx.seed(["indirim"])[0]["targets"] == ["discount"]
    assert lx.associations("indirim") == [] and lx.text_hits("indirim") == []


def test_show_reports_pairs_seed_and_sites(shop):
    lexicon.build(shop)
    out = lexicon.show(shop, "siparişi")
    assert out["pairs"] and out["lexicon"]["pairs"] > 0 and "siparisi" in out["keys"][0] + "siparisi"
    assert out["seed"][0]["targets"] == ["order"]


def test_non_python_code_uses_graph_symbols(tmp_path):
    root = tmp_path / "js"
    (root / "src").mkdir(parents=True)
    (root / "src" / "cart.js").write_bytes(
        b"// sepet islemleri\nfunction addToCart(item) {\n  // sepete urun ekler\n  return item;\n}\n")
    G = nx.MultiDiGraph()
    G.add_node("f", label="cart.js", source_file="src/cart.js", file_type="code", source_location="L1")
    G.add_node("s", label="addToCart()", source_file="src/cart.js", file_type="code", source_location="L2")
    g = Graph(G=G, path=root / "graph.json", root=root)
    g._spans["s"] = (2, 5)
    lexicon.build(root, g)
    raw = json.loads(lexicon.lexicon_path(root).read_text(encoding="utf-8"))
    units = raw["files"]["src/cart.js"]["units"]
    assert units and units[0][0] == "src/cart.js:2" and "cart" in units[0][2]
    assert any(w.startswith("sepet") for w in units[0][1])
