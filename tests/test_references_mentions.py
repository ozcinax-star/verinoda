"""Mention extraction (verinoda.references.mentions): TR/EN, span-exclusive, suffix-aware, no network."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from verinoda.references.mentions import (
    KINDS,
    QUALIFIER_KINDS,
    REFERENCE_KINDS,
    clause_of,
    clauses,
    extract,
)

POC = ("requests'in 2.31 sürümünde https://github.com/psf/requests/blob/main/src/requests/sessions.py#L100-L120 "
       "satırlarındaki yönlendirme mantığı bizimkinden farklı mı?")


def kinds(text):
    return [(m["kind"], m["text"]) for m in extract(text)]


def test_poc_sentence():
    got = extract(POC)
    assert [(m["kind"], m["text"]) for m in got] == [
        ("name_candidate", "requests"), ("version", "2.31"),
        ("url", "https://github.com/psf/requests/blob/main/src/requests/sessions.py#L100-L120")]
    assert got[0]["stripped_suffix"] == "'in" and got[0]["confidence"] == "heuristic"
    assert [m["id"] for m in got] == ["m1", "m2", "m3"]
    for m in got:
        assert POC[m["span"][0]:m["span"][1]] == m["text"]  # spans index the ORIGINAL text


@pytest.mark.parametrize("text, expect", [
    ("psf/requests@147c851 commit'indeki değişiklik psf/requests#6963 ile mi geldi?",
     [("repo_at", "psf/requests@147c851"), ("xref_issue", "psf/requests#6963")]),
    ("See arXiv:1706.03762 and doi:10.1145/3368089.3409747.",
     [("arxiv", "arXiv:1706.03762"), ("doi", "doi:10.1145/3368089.3409747")]),
    ("VS Code'un yaptığı gibi; golang.org/x/net@v0.20.0 ve requests==2.31.0 ile pkg:npm/react@18.2.0 kullanıyoruz.",
     [("app", "VS Code"), ("gomod", "golang.org/x/net@v0.20.0"), ("pkgspec", "requests==2.31.0"),
      ("purl", "pkg:npm/react@18.2.0"), ("local_pin", "kullanıyoruz")]),
    ("The fix landed in March 2024 on main, see swh:1:rev:6e83187b8feb273ed4c6cdab5efd8d54901dfab3 and GH-6000.",
     [("date", "March 2024"), ("floating", "on main"),
      ("swhid", "swh:1:rev:6e83187b8feb273ed4c6cdab5efd8d54901dfab3"), ("xref_issue", "GH-6000")]),
    ("Like VS Code does it, how does our watcher debounce?", [("app", "VS Code")]),
    ("Upgrade to the latest release; PR #6963 and !12 and MR 4",
     [("floating", "latest"), ("xref_pr", "PR #6963"), ("xref_mr", "!12"), ("xref_mr", "MR 4")]),
    ("requests 2.31 ve django 4.2 arasında fark var mı? Python 3.11 kullanıyoruz.",
     [("name_candidate", "requests"), ("version_candidate", "2.31"), ("name_candidate", "django"),
      ("version_candidate", "4.2"),
      ("name_candidate", "Python"), ("version", "3.11"), ("local_pin", "kullanıyoruz")]),
    ("Check `Session.send` in src/requests/sessions.py'deki kod ve compute_total() fonksiyonu",
     [("symbol", "Session.send"), ("file", "src/requests/sessions.py"), ("symbol", "compute_total()")]),
    ("eski sürümde böyle değildi, 2024-03-15 tarihinde değişti",
     [("version_candidate", "eski sürümde"), ("date", "2024-03-15")]),
    ("the version we use of numpy vs main'deki kod", [("local_pin", "the version we use"), ("floating", "main")]),
    ("15 Mart 2024'te çıkan sürüm; react@^18.2.0 ile org.apache.commons:commons-lang3:3.14.0",
     [("date", "15 Mart 2024"), ("pkgspec", "react@^18.2.0"), ("maven_gav", "org.apache.commons:commons-lang3:3.14.0")]),
    ("a bare 3.11 here", [("name_candidate", "bare"), ("version_candidate", "3.11")]),
    ("it is about 2.5 times faster", [("version_candidate", "2.5")]),
    ("version 3.11 and v2 and 1.2.3", [("version", "3.11"), ("version", "v2"), ("version", "1.2.3")]),
    ("İndirim kuralı hangi dosyada?", []),
])
def test_extraction_table(text, expect):
    assert kinds(text) == expect


def test_url_inside_angle_brackets_and_trailing_punctuation():
    got = extract("(see <https://example.com/a/b>.) and https://github.com/o/r/pull/7).")
    assert [m["text"] for m in got] == ["https://example.com/a/b", "https://github.com/o/r/pull/7"]


def test_turkish_suffix_after_url_and_number():
    got = extract("https://github.com/o/r/issues/5'teki yorum ve #12'de")
    assert got[0]["text"] == "https://github.com/o/r/issues/5" and got[0]["stripped_suffix"] == "'teki"
    assert got[1]["kind"] == "xref_issue" and got[1]["text"] == "#12" and got[1]["stripped_suffix"] == "'de"


def test_dates_normalise_to_ranges():
    m = extract("in Aralık 2023")[0]
    assert m["normalized"] == {"date_from": "2023-12-01", "date_to": "2023-12-31", "relative": False}
    m = extract("since March 15, 2024")[0]
    assert m["normalized"]["date_from"] == "2024-03-15"


def test_clauses_ignore_dots_inside_mentions():
    text = "requests 2.31.0 is at https://x.org/a.b/c. Then numpy."
    ms = extract(text)
    segs = clauses(text, ms)
    assert len(segs) == 2
    assert clause_of(segs, ms[0]["span"][0]) == clause_of(segs, ms[-1]["span"][0])


def test_kind_sets_are_consistent():
    assert REFERENCE_KINDS.isdisjoint(QUALIFIER_KINDS) and KINDS == REFERENCE_KINDS | QUALIFIER_KINDS


_ALPHABET = st.sampled_from(list("abcçdeğıiöşüv0123456789 ./:#@!'’-_=`()<>\n") + [
    "https://github.com/o/r/blob/main/x.py#L1-L2", "requests==2.31.0", "2.31", "v1.2.3", "psf/requests#12",
    "arXiv:1706.03762v2", "10.1145/1.2", "'in", "'deki", " main'de ", "en son", "Mart 2024", "PR 12",
    "pkg:pypi/x@1", "`a.b`", "swh:1:rev:" + "a" * 40])


@settings(max_examples=250, deadline=None)
@given(st.lists(_ALPHABET, max_size=40).map("".join))
def test_extraction_invariants(text):
    ms = extract(text)
    spans = [tuple(m["span"]) for m in ms]
    assert spans == sorted(spans)
    for (a, b), (c, d) in zip(spans, spans[1:]):
        assert b <= c  # span-exclusive
    for m in ms:
        a, b = m["span"]
        assert 0 <= a < b <= len(text)
        assert text[a:b] == m["text"]
        assert m["kind"] in KINDS and m["extractor"] and m["confidence"] in ("exact", "pattern", "heuristic")
    assert [m["id"] for m in ms] == [f"m{i}" for i in range(1, len(ms) + 1)]
    assert [(m["kind"], m["span"]) for m in extract(text)] == [(m["kind"], m["span"]) for m in ms]  # deterministic
