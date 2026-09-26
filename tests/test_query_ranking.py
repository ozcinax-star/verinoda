"""Query ranking (docs/DESIGN.md D-query-ranking): tests yield to the code that matches the same words,
files and modules the question names count more, docstring phrases count, and expansions stay narrow."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import index, search_index, workflow
from verinoda.store import open_store

CLIENT = '''class Connection:
    def request(self, method, url):
        """Send one request over the connection."""
        return self._send(method, url)

    def _send(self, method, url):
        return method, url


def read_chunked(stream):
    """Read a chunked response body until the last chunk."""
    size = stream.chunk_size()
    return stream.read(size)
'''

OTHER = '''def client(addr):
    """Open a client for an address."""
    return addr


def request(url):
    return url
'''

SHELL = '''def list2cmdline(seq):
    """Translate a sequence of arguments into a command line string."""
    return " ".join(seq)


def split_args(text):
    # a string holds the command, then the line of arguments
    return text.split()
'''

CLUSTER = '''def split_community(nodes):
    """Split an oversized community in two."""
    half = len(nodes) // 2
    return nodes[:half], nodes[half:]
'''

COMMUNITIES = '''def split_oversized_community(nodes):
    """Split an oversized community."""
    return [nodes]
'''

TESTS = '''from pkg.net.client import read_chunked


class FakeStream:
    def chunk_size(self):
        return 0

    def read(self, n):
        return b""


def test_read_chunked_response_body_reads_every_chunk():
    """A chunked response body is read chunk by chunk."""
    assert read_chunked(FakeStream()) == b""


def test_unicode_roundtrip_survives_everything():
    assert "unicode roundtrip" == "unicode roundtrip"
'''


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def g(tmp_path_factory):
    root = tmp_path_factory.mktemp("qrank") / "proj"
    _write(root, "pkg/__init__.py", '"""The package."""\n')
    _write(root, "pkg/net/__init__.py", "")
    _write(root, "pkg/net/client.py", CLIENT)
    _write(root, "pkg/other.py", OTHER)
    _write(root, "pkg/shell.py", SHELL)
    _write(root, "pkg/cluster.py", CLUSTER)
    _write(root, "examples/raw/cluster.py", CLUSTER)  # a copy of it
    _write(root, "pkg/communities.py", COMMUNITIES)
    _write(root, "tests/test_client.py", TESTS)
    _write(root, "pkg/money.py", "def para_birimi(para):\n    return para\n\n\ndef parameter_count(params):\n"
                                 "    return len(params)\n\n\ndef parallel_map(fn, xs):\n    return [fn(x) for x in xs]\n\n\n"
                                 "def cli_main(argv):\n    return argv\n")
    for k in range(12):  # more files, so one package is not most of the project
        _write(root, f"lib/mod{k}.py", f"def helper_{k}(x):\n    return x + {k}\n")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    return index.load(root)


def _hits(g, q):
    return search_index.rank(g, q).hits


def _first(hits, pred):
    return next(k for k, h in enumerate(hits) if pred(h))


def _expansions(g, q):
    conn = sqlite3.connect(search_index.db_path_for(g))
    try:
        return search_index.analyze_query(q, conn, repo=g.root).expansions
    finally:
        conn.close()


# -- what the question asks -----------------------------------------------------------------------

def test_questions_about_tests_and_about_callers_are_recognised():
    tests = ["Which tests cover the discount?", "what does test_cluster check?", "is HeatMathTest run in CI?",
             "İndirimi hangi testler çalıştırıyor?", "what needs retesting if save changes?", "coverage of pricing"]
    assert all(search_index.asks_about_tests(q) for q in tests)
    assert not any(search_index.asks_about_tests(q) for q in ["where is the latest version read?",
                                                              "how does the contest scoring work?"])
    callers = ["Who calls Wisp.spawn?", "What would be affected if I change rank?", "what calls assess_change?",
               "Wisp.spawn kimler tarafından çağrılıyor?", "OrderRepository.save değişirse ne olur?",
               "What needs retesting if OrderRepository.save changes?"]
    assert all(search_index.asks_about_callers(q) for q in callers)
    assert not search_index.asks_about_callers("where does subprocess build the command line?")


# -- tests yield to the code they test --------------------------------------------------------------

def test_a_test_ranks_below_the_code_that_matches_the_same_words(g):
    hits = _hits(g, "how is a chunked response body read?")
    code = _first(hits, lambda h: h.name == "read_chunked")
    test = _first(hits, lambda h: h.name.startswith("test_read_chunked"))
    assert code < test


def test_a_question_about_tests_puts_the_tests_first(g):
    hits = _hits(g, "which tests cover reading a chunked response body?")
    assert search_index.is_test_file(hits[0].file) and hits[0].name.startswith("test_read_chunked")


def test_a_test_that_is_the_only_match_keeps_its_rank(g):
    hits = _hits(g, "does the unicode roundtrip survive?")
    assert hits[0].name == "test_unicode_roundtrip_survives_everything"


def test_a_callers_question_leaves_tests_where_the_index_put_them(g, monkeypatch):
    q = "who calls read_chunked?"
    before = {h.key: h.score for h in _hits(g, q)}
    monkeypatch.setattr(search_index, "TEST_FACTOR", 0.1)
    after = {h.key: h.score for h in _hits(g, q)}
    tests = [k for k in before if "test_client" in k]
    assert tests and all(after[k] == before[k] for k in tests)


# -- files and modules the question names -----------------------------------------------------------

def test_a_path_in_the_question_ranks_that_file_first(g):
    plain = _hits(g, "how is an oversized community split?")
    assert plain[0].file == "pkg/communities.py"  # its name carries more of the words
    hits = _hits(g, "how does pkg/cluster.py split an oversized community?")
    assert hits[0].file == "pkg/cluster.py" and "question names pkg/cluster.py" in hits[0].reasons
    files = [h.file for h in hits]
    assert files.index("pkg/communities.py") > files.index("pkg/cluster.py")
    assert "examples/raw/cluster.py" not in files[:2]  # a copy under another path is not named


def test_a_dotted_module_names_its_file_not_every_symbol_of_that_name(g):
    hits = _hits(g, "What would be affected if I change net.client.Connection.request?")
    assert (hits[0].file, hits[0].name) == ("pkg/net/client.py", "request")
    assert "question names net.client" in hits[0].reasons
    # the module part "client" is not the function client() of another file, nor is its request()
    other = [h for h in hits if h.file == "pkg/other.py"]
    assert all("question names 'client'" not in h.reasons and "question names 'request'" not in h.reasons
               for h in other)


def test_a_plain_word_that_is_a_file_stem_counts_a_little(g, monkeypatch):
    q = "where does shell split the arguments?"
    hits, factor = _hits(g, q), search_index.FILE_WORD_FACTOR
    # no "question names" reason for a plain word: it only counts a little
    assert not [h for h in hits if any(r.startswith("question names") for r in h.reasons)]
    boosted = {(h.file, h.name): h.lex for h in hits}
    monkeypatch.setattr(search_index, "FILE_WORD_FACTOR", 1.0)
    plain = {(h.file, h.name): h.lex for h in _hits(g, q)}
    shell = [k for k in boosted if k[0] == "pkg/shell.py" and k in plain]
    assert factor > 1.0 and shell and all(boosted[k] == pytest.approx(plain[k] * factor) for k in shell)
    assert all(boosted[k] == pytest.approx(plain[k]) for k in boosted if k[0] != "pkg/shell.py" and k in plain)


def test_a_package_holding_most_of_the_project_names_nothing(g, monkeypatch):
    h = search_index.open_for(g)
    monkeypatch.setattr(search_index, "MAX_NAMED_SHARE", 0.01)
    assert not search_index._mentions("where does pkg split args?", h, False).files
    monkeypatch.setattr(search_index, "MAX_NAMED_SHARE", 0.9)
    assert "pkg/shell.py" in search_index._mentions("where does pkg split args?", h, False).files


# -- docstring phrases ------------------------------------------------------------------------------

def test_a_docstring_that_writes_the_question_words_side_by_side_counts_more(g, monkeypatch):
    q = "which function builds the command line string?"
    with_doc = {h.name: h.lex for h in _hits(g, q)}
    monkeypatch.setattr(search_index, "DOC_PHRASE_BOOST", 0.0)
    without = {h.name: h.lex for h in _hits(g, q)}
    assert with_doc["list2cmdline"] > without["list2cmdline"]
    assert with_doc.get("split_args", 0.0) == without.get("split_args", 0.0)  # its words are only in a comment


# -- expansions ---------------------------------------------------------------------------------------

def test_a_word_the_code_names_things_with_is_not_taken_for_an_abbreviation(g):
    # cli_main makes "cli" a corpus prefix of "client", but client() is a name of its own
    assert not [e for e in _expansions(g, "how does the client open?") if e["to"] == "cli"]


def test_turkish_stems_do_not_reach_other_words_or_expand_twice(g):
    exps = _expansions(g, "Parayı kim hesaplıyor?")
    to = {e["to"] for e in exps}
    assert {"para", "para_birimi"} <= to
    assert not to & {"paramet", "param", "parallel", "par"}
