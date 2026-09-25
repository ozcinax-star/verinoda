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
    assert search_index.tokens("_query_terms") == ["query_terms", "queri", "term"]  # query/queries/queried: one stem
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


def test_near_and_query_pairs():
    a, b = frozenset({"hom"}), frozenset({"directory"})
    assert search_index._near(["the", "hom", "directory"], a, b)
    assert search_index._near(["directory", "hom"], a, b)                    # either order
    assert not search_index._near(["directory", "of", "hom"], a, b)          # adjacent only (gap 1)
    assert not search_index._near(["hom", "x", "y", "z", "directory"], a, b)
    pairs = search_index._query_pairs(["home", "directory", "resolution"])
    assert pairs == [(frozenset({"hom"}), frozenset({"directori"})),
                     (frozenset({"directori"}), frozenset({"resolution"}))]
    assert search_index._query_pairs(["home"]) == []


def test_adjacent_question_words_lift_the_passage_that_uses_them_together(tmp_path):
    """Found by running Verinoda on itself: "home directory" ranked the function whose
    docstring says "The home directory ..." below functions that merely repeat a
    ``home`` parameter and mention a directory somewhere else."""
    root = tmp_path / "prox"
    _write(root, "pkg/installer.py", (
        "def copy_files(home, target, home_backup, home_cache):\n"
        "    '''Copy home files, home backups and home caches.'''\n"
        "    for name in (home, home_backup, home_cache):\n"
        "        print('home', name, target)\n"
        "    # the target directory must exist\n"
        "    return target\n"))
    _write(root, "pkg/paths.py", (
        "def find_root(start):\n"
        "    '''The nearest project above start; the home directory is never used.'''\n"
        "    return start\n"))
    g = _scan(root)
    rk = search_index.rank(g, "home directory", ppr=False)
    assert rk.hits[0].name == "find_root", [(h.name, round(h.score, 3)) for h in rk.hits[:3]]


def test_no_file_cap_every_indexed_file_is_searchable(tmp_path):
    """The old per-query scan silently stopped after 400 files."""
    root = tmp_path / "many"
    for i in range(420):
        _write(root, f"pkg/m{i:03d}.py", f"def helper_{i:03d}():\n    return {i}\n")
    _write(root, "pkg/zz_last.py", "def unique_needle_function():\n    return 'needle'\n")
    g = _scan(root)
    rk = search_index.rank(g, "unique needle")
    assert rk.hits[0].file == "pkg/zz_last.py"


def test_locale_labels_expand_at_full_weight_instead_of_their_words(mini, monkeypatch):
    runner_tok = search_index.tokens("runner")[0]

    class FakeLexicon:
        def phrase_hits(self, words):
            return [{"start": i, "n": 2, "key": "kor ocagi", "targets": ["runner"], "sites": []}
                    for i in range(len(words) - 1) if words[i] == "kor" and words[i + 1].startswith("ocag")]

        def associations(self, word):
            return [{"part": "retries", "score": 0.9}] if word in ("kor", "ocagi") else []

        def seed(self, words):
            return []

    fake = types.ModuleType("verinoda.lexicon")
    fake.load = lambda repo: FakeLexicon()
    monkeypatch.setitem(sys.modules, "verinoda.lexicon", fake)
    monkeypatch.setattr(verinoda, "lexicon", fake, raising=False)
    q = _query(mini, "Kor Ocağı nerede çalışıyor?", repo=mini.root)
    assert {"from": "kor ocagi", "to": runner_tok, "via": "locale label", "weight": 1.0} in q.expansions
    assert not [e for e in q.expansions if e["from"] in ("kor", "ocagi")]  # the label replaces its words


def test_a_qualified_name_floors_the_method_of_that_owner_only(tmp_path):
    root = tmp_path / "qual"
    # the class's best passages are its comments near the top, not the spawn method at its end
    filler = "".join(f"    def helper{i}(self):\n        # keeps the wisp lit\n        return {i}\n\n"
                     for i in range(30))
    _write(root, "game/wisp.py", "class Wisp:\n" + filler + "    def spawn(self, world):\n        return world\n")
    _write(root, "game/ghost.py", "class Ghost:\n    def spawn(self, world):\n        return world\n")
    _write(root, "game/rituals.py", "from game.wisp import Wisp\n\n\ndef summon(world):\n"
                                    "    return Wisp().spawn(world)\n")
    g = _scan(root)
    hits = search_index.rank(g, "Who calls Wisp.spawn?").hits
    floored = {h.qual for h in hits if "question names 'spawn'" in h.reasons}
    assert floored == {"Wisp.spawn"}
    # a module written before a function counts as its owner too
    assert "summon" in {h.name for h in search_index.rank(g, "what does rituals.summon do?").hits
                        if "question names 'summon'" in h.reasons}
    # the method of a long class gets its own section (with its callers) after the class's
    text = retrieval.render_text(retrieval.retrieve(g, "Who calls Wisp.spawn?"), 6000)
    assert "## game/wisp.py:122-123 def spawn(self, world)" in text
    assert "called by: summon (game/rituals.py:5)" in text


def test_two_adjacent_words_the_code_writes_as_one_name_count_as_that_name(mini):
    q = _query(mini, "Where is the needle marker?")
    assert {"from": "needle marker", "to": "needle_marker", "via": "joined words", "weight": 1.0} in q.expansions
    tr = _query(mini, "Sipariş oluşturma nerede?")  # a confirmed stem of each word joins too
    assert any(e["to"] == "siparis_olustur" and e["via"] == "joined words" for e in tr.expansions)
    assert not [e for e in _query(mini, "Which environment variables?").expansions if e["via"] == "joined words"]
    # one dotted token ("needle.marker", like graph.json) is not a two-word phrase
    assert not [e for e in _query(mini, "Where is needle.marker set?").expansions if e["via"] == "joined words"]


def test_the_stem_most_units_use_wins_over_an_english_stem_artifact(tmp_path):
    # "melekte" is indexed as "melekt" (a final e stripped); "melekten" still means melek
    root = tmp_path / "stem"
    _write(root, "a/melek.py", "".join(f"def melek_{i}():\n    return {i}\n\n" for i in range(6)))
    _write(root, "a/notes.py", '"""Bu not melekte kalir."""\n')
    g = _scan(root)
    q = _query(g, "Melekten ne kalıyor?")
    stems = [e["to"] for e in q.expansions if str(e["via"]).startswith("turkish stem")]
    assert stems[:1] == ["melek"]


def test_the_longest_stem_wins_over_a_shorter_more_common_english_word(tmp_path):
    root = tmp_path / "even"
    _write(root, "a/events.py", "def dispatch_event(event):\n    return event\n")
    _write(root, "a/maths.py", "".join(f"def is_even_{i}(n):\n    return n % 2 == 0  # even\n\n" for i in range(8)))
    g = _scan(root)
    stems = [e["to"] for e in _query(g, "Eventleri hangi fonksiyon işliyor?").expansions
             if str(e["via"]).startswith("turkish stem")]
    assert stems[:1] == ["event"]


def test_a_module_qualifier_is_the_module_not_a_class_of_that_name(tmp_path):
    root = tmp_path / "owner"
    _write(root, "store/__init__.py", "def save(record):\n    return record\n")
    _write(root, "cache/memory.py", "class Store:\n    def save(self, record):\n        return record\n")
    _write(root, "app/main.py", "import store\n\n\ndef run(r):\n    return store.save(r)\n")
    g = _scan(root)
    floored = {h.file for h in search_index.rank(g, "Who calls store.save?").hits if "question names 'save'" in h.reasons}
    assert floored == {"store/__init__.py"}
    floored = {h.file for h in search_index.rank(g, "Who calls Store.save?").hits if "question names 'save'" in h.reasons}
    assert floored == {"cache/memory.py"}


@pytest.mark.parametrize("family", [("query", "queries", "queried", "querying"), ("copy", "copies", "copied"),
                                    ("entry", "entries"), ("dependency", "dependencies"), ("retry", "retries"),
                                    ("key", "keys"), ("play", "plays"), ("save", "saved", "saves"),
                                    ("cluster", "clusters", "clustered", "clustering"), ("filter", "filtered", "filtering"),
                                    ("register", "registered", "registering"), ("render", "rendered", "rendering"),
                                    ("order", "orders", "ordered", "ordering"), ("power", "powered")])
def test_a_word_and_its_inflections_are_one_token(family):
    """A question saying "query" meets code saying "queries" (they were quer / query before), and
    "clustered" meets "cluster" (clust; the inflected form kept its -er before)."""
    assert len({search_index.word_stem(w) for w in family}) == 1
