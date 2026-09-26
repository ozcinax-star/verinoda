"""English questions over code named in Turkish (docs/DESIGN.md D55): a symbol's own javadoc is its unit's text,
and the seed dictionary read backwards reaches Turkish name parts with their inflections."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index, search_index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

SHOP = """package com.example.shop;

import java.util.List;

public final class Dukkan {
    /** The shop's opening hours, from the config. */
    static int saat;

    /**
     * Stores the order in the ledger and sends the receipt; an order without lines is refused.
     */
    public static void siparisKaydet(Object siparis) {
        defter(siparis);
    }

    /** Every hostile creature within the given radius of the counter. */
    public static List<Object> dusmanlar(Object tezgah, double yaricap) {
        return List.of();
    }

    /** Writes one line to the ledger. */
    static void defter(Object satir) {
    }
}
"""


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("trnames") / "shop"
    p = root / "src/main/java/com/example/shop/Dukkan.java"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(SHOP.encode("utf-8"))
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    search_index._HANDLES.clear()
    return root


def _rank(repo: Path, q: str) -> list[str]:
    return [(h.name or "").strip(".()") for h in search_index.rank(index.load(repo), q, limit=10).hits]


def test_a_methods_javadoc_is_its_own_text(repo):
    g = index.load(repo)
    h = search_index.open_for(g)
    conn = h.connect()
    try:
        rows = conn.execute("SELECT u.name, p.a, p.b FROM units u JOIN passages p ON p.uid = u.uid").fetchall()
    finally:
        h.release(conn)
    spans = {}
    for name, a, b in rows:
        spans.setdefault(name.strip(".()"), []).append((a, b))
    lines = SHOP.splitlines()
    doc = next(i for i, ln in enumerate(lines, 1) if "Stores the order" in ln)
    assert any(a <= doc <= b for a, b in spans["siparisKaydet"])          # the method's passage holds its javadoc
    assert not any(a <= doc <= b for a, b in spans.get("Dukkan", []))     # the class's does not any more


def test_english_words_reach_turkish_names(repo):
    assert _rank(repo, "save the order")[0] == "siparisKaydet"
    assert _rank(repo, "enemies near the counter")[0] == "dusmanlar"      # dusman + lar, as the index cuts it
    g = index.load(repo)
    h = search_index.open_for(g)
    conn = h.connect()
    try:
        q = search_index.analyze_query("save the order", conn, repo=g.root)
    finally:
        h.release(conn)
    vias = {e["via"] for e in q.expansions}
    assert any(v.startswith("seed translation") for v in vias)
