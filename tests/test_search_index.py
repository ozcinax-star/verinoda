"""Persistent passage index (search.db): tokenizer, units/passages, exact incremental updates,
BM25F with per-unit idf, the PageRank prior, query-side expansions and staleness."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

import networkx as nx  # noqa: E402
import pytest  # noqa: E402

import verinoda  # noqa: E402

from verinoda import index, retrieval, search_index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

CORE = '''"""Core module of the mini app."""
import os

ENV_ALLOW = {"PATH", "HOME"}
MAX_RETRIES = 3


def _scrubbed_env():
    """Keep only the allowed environment variables."""
    return {k: v for k, v in os.environ.items() if k in ENV_ALLOW}


class Runner:
    def run(self, cmd):
        env = _scrubbed_env()
        for attempt in range(MAX_RETRIES):
            print(attempt, cmd)
        return env


def long_function(x):
''' + "".join(f"    step_{i} = x + {i}\n" for i in range(28)) + '''    needle_marker = "rare"
    return x
'''

SIPARIS = '''def siparis_olustur(musteri, kalemler):
    """Yeni bir siparis kaydi olusturur."""
    return {"musteri": musteri, "kalemler": kalemler}
'''

GUIDE = """# Guide

Intro text about the runner.

## Running

Call `Runner.run` with a command.

### Details

Retries are bounded by MAX_RETRIES.

## Other

Nothing here.
"""


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _scan(root: Path) -> index.Graph:
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    return index.load(root)


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    root = tmp_path_factory.mktemp("sidx") / "mini"
    _write(root, "app/core.py", CORE)
    _write(root, "app/siparis.py", SIPARIS)
    _write(root, "docs/guide.md", GUIDE)
    return _scan(root)


def _units(g) -> list[dict]:
    conn = sqlite3.connect(search_index.db_path_for(g))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM units ORDER BY file, a")]
    finally:
        conn.close()


# -- tokenizer --------------------------------------------------------------------------------------

def test_tokens_are_identifier_aware_and_folded():
    assert search_index.tokens("getOrderRepo_v2") == ["getorderrepo_v2", "get", "order", "repo", "v2"]
    assert search_index.tokens("_query_terms") == ["query_terms", "query", "term"]
    # Turkish letters are folded first; the light English stem then applies to every word alike
    assert search_index.tokens("İndirim ŞİPARİŞ siparis") == ["indirim", "sipari", "sipari"]
    assert search_index.tokens("x = 1") == []  # single characters are not tokens
    assert search_index.stem("environment") == "environ" and search_index.stem("variables") == "variabl"
    assert search_index.stem("_query_terms") == "_query_terms"
    # inflections meet their base form; a final 'e' never turns a word into a common short one
    assert {search_index.word_stem(w) for w in ("save", "saved", "saves")} == {"sav"}
    assert search_index.word_stem("cached") == search_index.word_stem("cache") == "cach"
    assert search_index.word_stem("note") == "note" and search_index.word_stem("use") == "use"
    assert search_index.tokens("saveOrder saved_orders") == ["saveorder", "sav", "order", "saved_orders", "sav", "order"]


def test_named_identifiers_skip_file_names():
    assert search_index.named_identifiers("Is OrderRepository.save or compute_total() in graph.json?") == \
        ["OrderRepository", "save", "compute_total"]


# -- units and passages -------------------------------------------------------------------------------

def test_units_cover_symbols_module_blocks_and_prose_sections(mini):
    units = _units(mini)
    kinds = {(u["file"], u["kind"], u["name"]) for u in units}
    assert ("app/core.py", "symbol", "_scrubbed_env") in kinds and ("app/core.py", "symbol", "run") in kinds
    assert ("app/core.py", "module", "ENV_ALLOW MAX_RETRIES") in kinds
    assert {("docs/guide.md", "prose", n) for n in ("Guide", "Running", "Details", "Other")} <= kinds
    run = next(u for u in units if u["name"] == "run")
    assert run["qual"] == "Runner.run" and run["sig"] == "def run(self, cmd)"
    scrub = next(u for u in units if u["name"] == "_scrubbed_env")
    assert scrub["doc"] == "Keep only the allowed environment variables."
    assert json.loads(scrub["consts"]) == [["ENV_ALLOW", 4]]
    cls = next(u for u in units if u["name"] == "Runner")
    assert cls["own"] == 1  # the class line; its method's lines belong to the method


def test_long_units_become_overlapping_passages(mini):
    conn = sqlite3.connect(search_index.db_path_for(mini))
    try:
        uid, a = conn.execute("SELECT uid, a FROM units WHERE name = 'long_function'").fetchone()
        ps = conn.execute("SELECT a, b FROM passages WHERE uid = ? ORDER BY a", (uid,)).fetchall()
    finally:
        conn.close()
    # 31 own lines: windows of 12 with a stride of 6
    assert ps == [(a, a + 11), (a + 6, a + 17), (a + 12, a + 23), (a + 18, a + 29), (a + 24, a + 30)]


def test_document_frequency_is_counted_per_unit(mini):
    conn = sqlite3.connect(search_index.db_path_for(mini))
    try:
        df = dict(conn.execute("SELECT term, df FROM df WHERE term IN ('step', 'needle_marker')").fetchall())
    finally:
        conn.close()
    assert df == {"step": 1, "needle_marker": 1}  # 28 lines and 5 passages, one unit


# -- ranking -------------------------------------------------------------------------------------------

def test_best_passage_wins_and_named_identifiers_score_one(mini):
    rk = search_index.rank(mini, "Where is the needle marker?")
    assert rk.hits[0].name == "long_function" and rk.hits[0].lex == 1.0
    best = rk.hits[0].passages[0]
    assert best[2] <= 50 <= best[3] or "needle_marker" in (mini.root / "app/core.py").read_text().splitlines()[best[2] - 1:best[3]][-1]
    named = search_index.rank(mini, "what calls _scrubbed_env?")
    first = named.hits[0]
    assert first.name == "_scrubbed_env" and first.lex == 1.0 and "question names '_scrubbed_env'" in first.reasons


def test_pagerank_prior_reaches_callers_without_the_question_words(mini):
    rk = search_index.rank(mini, "_scrubbed_env")
    run = next(h for h in rk.hits if h.qual == "Runner.run")
    assert run.ppr > 0 and run.score >= run.lex
    no_ppr = search_index.rank(mini, "_scrubbed_env", ppr=False)
    assert all(h.ppr == 0 for h in no_ppr.hits)


def test_personalized_pagerank_is_local_deterministic_and_weights_inferred_edges_less(tmp_path):
    G = nx.MultiDiGraph()
    for n in "abcde":
        G.add_node(n, label=n, source_file="m.py", source_location="L1", file_type="code")
    G.add_edge("a", "b", relation="calls", confidence="EXTRACTED")
    G.add_edge("a", "c", relation="calls", confidence="INFERRED")
    G.add_edge("b", "d", relation="calls", confidence="EXTRACTED")
    g = index.Graph(G=G, path=tmp_path / "graph.json", root=tmp_path)
    nid_uid = {n: i for i, n in enumerate("abcd")}  # "e" has no unit: never reached
    p1, pushes = search_index.personalized_pagerank(g, {"a": 1.0}, nid_uid)
    p2, _ = search_index.personalized_pagerank(g, {"a": 1.0}, nid_uid)
    assert p1 == p2 and pushes > 0 and "e" not in p1
    assert p1["a"] > p1["b"] > p1["c"] > 0 and p1["d"] > 0
    assert abs(sum(p1.values()) - 1.0) < 0.05
    limited, n = search_index.personalized_pagerank(g, {"a": 1.0}, nid_uid, max_push=1)
    assert n == 1 and set(limited) == {"a"}


# -- query side --------------------------------------------------------------------------------------

def _query(g, q, **kw):
    conn = sqlite3.connect(search_index.db_path_for(g))
    try:
        return search_index.analyze_query(q, conn, **kw)
    finally:
        conn.close()


def test_query_drops_english_and_turkish_stopwords_and_apostrophe_suffixes(mini):
    q = _query(mini, "Hangi fonksiyon core.py'deki ENV_ALLOW listesini nerede kullanıyor?")
    assert "nerede" in q.dropped and "hangi" in [d.lower() for d in q.dropped] and "'deki" in q.dropped
    assert "env_allow" in q.weights and "deki" not in q.weights
    q2 = _query(mini, "Which function reads the environment?")
    assert "which" in [d.lower() for d in q2.dropped] and "environ" in q2.weights


def test_abbreviations_are_expanded_and_reported(mini):
    q = _query(mini, "Which environment variables are allowed?")
    exp = {(e["from"], e["to"], e["via"]) for e in q.expansions}
    assert ("environ", "env", "abbreviation") in exp
    assert q.weights["env"] == search_index.EXPANSION_WEIGHT < q.weights["environ"] == 1.0
    res = retrieval.retrieve(mini, "Which environment variables are allowed?")
    assert any(e.startswith("environ->env") for e in res["budget"]["expansions"])
    assert res["items"][0]["symbol"] in ("_scrubbed_env()", "(module level)")


def test_corpus_prefixes_need_a_symbol_named_with_them(mini):
    # "retr" is only the stem of MAX_RETRIES, not a name part written in the code
    q = _query(mini, "retrieval running")
    assert not [e for e in q.expansions if e["via"] == "corpus prefix"]
    q2 = _query(mini, "maximum attempts")  # "max" is a name part written in the code (MAX_RETRIES)
    assert {"from": "maximum", "to": "max", "via": "corpus prefix",
            "weight": search_index.EXPANSION_WEIGHT} in q2.expansions
    assert search_index.NOT_ABBREVIATIONS >= {"def", "over", "out"}


def test_turkish_words_unknown_to_the_index_use_a_vocabulary_confirmed_stem(mini):
    q = _query(mini, "Siparişleri nerede oluşturuyoruz?")
    assert any(e["to"].startswith("siparis") and e["via"].startswith("turkish stem") for e in q.expansions)
    rk = search_index.rank(mini, "Siparişleri nerede oluşturuyoruz?")
    assert rk.hits[0].name == "siparis_olustur"


def test_caller_expansions_and_the_lexicon_are_weighted_below_the_question(mini, monkeypatch):
    retr = search_index.tokens("retries")[0]
    q = _query(mini, "yeniden deneme sayısı nerede?", expansions={"deneme": ["retries"]})
    assert q.weights[retr] == search_index.PROVIDED_EXPANSION_WEIGHT
    assert {"from": "deneme", "to": retr, "via": "question plan",
            "weight": search_index.PROVIDED_EXPANSION_WEIGHT} in q.expansions

    class FakeLexicon:
        def associations(self, word):
            return [{"part": "retries", "score": 0.9}] if word.startswith("deneme") else []

        def seed(self, words):
            return [{"start": i, "n": 1, "key": w, "targets": ["runner"]} for i, w in enumerate(words)
                    if w.startswith("calistir")]

    fake = types.ModuleType("verinoda.lexicon")
    fake.load = lambda repo: FakeLexicon()
    monkeypatch.setitem(sys.modules, "verinoda.lexicon", fake)
    monkeypatch.setattr(verinoda, "lexicon", fake, raising=False)
    q2 = _query(mini, "denemeler nerede çalıştırılıyor?", repo=mini.root)
    got = {(e["from"], e["to"], e["via"]) for e in q2.expansions}
    assert ("denemeler", retr, "lexicon") in got and ("calistiriliyor", search_index.tokens("runner")[0], "lexicon") in got
    broken = types.ModuleType("verinoda.lexicon")
    broken.load = lambda repo: (_ for _ in ()).throw(ValueError("corrupt"))
    monkeypatch.setitem(sys.modules, "verinoda.lexicon", broken)
    monkeypatch.setattr(verinoda, "lexicon", broken, raising=False)
    assert _query(mini, "denemeler nerede?", repo=mini.root).weights  # never breaks retrieval


# -- incremental updates, versions, staleness -------------------------------------------------------

def test_incremental_update_equals_a_full_rebuild(tmp_path):
    root = tmp_path / "inc"
    _write(root, "app/core.py", CORE)
    _write(root, "app/siparis.py", SIPARIS)
    _write(root, "docs/guide.md", GUIDE)
    g = _scan(root)
    db = search_index.db_path_for(g)
    assert search_index.update(root, g)["mode"] == "noop"
    # modify one file, add one, delete one
    _write(root, "app/core.py", CORE.replace("MAX_RETRIES = 3", "MAX_RETRIES = 5\nTIMEOUT_S = 30"))
    _write(root, "app/extra.py", "def extra_helper():\n    return MAGIC_WORD\n")
    (root / "app" / "siparis.py").unlink()
    index.build(root, force=True)
    g2 = index.load(root)
    res = search_index.update(root, g2)
    assert res["mode"] == "incremental" and res["files_indexed"] == 2 and res["files_removed"] == 1
    fresh = tmp_path / "fresh.db"
    search_index.update(root, g2, rebuild=True, db=fresh)
    assert search_index.dump(db) == search_index.dump(fresh)
    rk = search_index.rank(g2, "extra helper magic word")
    assert rk.hits[0].name == "extra_helper"
    assert not any(h.file == "app/siparis.py" for h in search_index.rank(g2, "siparis olustur").hits)


def test_version_change_rebuilds_and_foreign_graph_is_synced_on_open(tmp_path):
    root = tmp_path / "ver"
    _write(root, "app/core.py", CORE)
    g = _scan(root)
    db = search_index.db_path_for(g)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = '0' WHERE key = 'tokenizer_version'")
    conn.commit()
    conn.close()
    assert search_index.update(root, g)["mode"] == "full"
    # the graph is rebuilt behind the index's back: opening it for a query syncs it
    _write(root, "app/more.py", "def brand_new_function():\n    return 1\n")
    index.build(root, force=True)
    g2 = index.load(root)
    h = search_index.open_for(g2)
    assert any("update" in n for n in h.notes)
    assert search_index.rank(g2, "brand_new_function").hits[0].name == "brand_new_function"


def test_unwritable_index_falls_back_to_memory(tmp_path, monkeypatch):
    root = tmp_path / "ro"
    _write(root, "app/core.py", CORE)
    g = _scan(root)
    search_index.db_path_for(g).unlink()
    search_index._HANDLES.clear()

    def refuse(*a, **k):
        raise PermissionError("read-only index directory")

    monkeypatch.setattr(search_index, "update", refuse)
    h = search_index.open_for(g)
    assert h.memory is not None and any("in memory" in n for n in h.notes)
    assert search_index.rank(g, "_scrubbed_env", handle=h).hits[0].name == "_scrubbed_env"
    search_index._HANDLES.clear()


def test_files_changed_after_indexing_are_reported(tmp_path):
    root = tmp_path / "stale"
    _write(root, "app/core.py", CORE)
    g = _scan(root)
    res = retrieval.retrieve(g, "_scrubbed_env")
    assert "stale_files" not in res["budget"]
    _write(root, "app/core.py", CORE + "\n# edited\n")
    res = retrieval.retrieve(g, "_scrubbed_env")
    assert res["budget"]["stale_files"] == ["app/core.py"]
    assert "changed since indexing" in retrieval.render_text(res)


def test_no_file_cap_every_indexed_file_is_searchable(tmp_path):
    """The old per-query scan silently stopped after 400 files."""
    root = tmp_path / "many"
    for i in range(420):
        _write(root, f"pkg/m{i:03d}.py", f"def helper_{i:03d}():\n    return {i}\n")
    _write(root, "pkg/zz_last.py", "def unique_needle_function():\n    return 'needle'\n")
    g = _scan(root)
    rk = search_index.rank(g, "unique needle")
    assert rk.hits[0].file == "pkg/zz_last.py"
