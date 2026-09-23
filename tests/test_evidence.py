"""Evidence: line-ending-stable content hashes and source re-checks (changed / moved / deleted)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import evidence as evmod  # noqa: E402
from verinoda.store import Store  # noqa: E402

SRC = (
    "import os\n"
    "\n"
    "def compute(x):\n"
    "    y = x * 2\n"
    "    return y + 1\n"
    "\n"
    "def other():\n"
    "    return 0\n"
)


def _write(p: Path, text: str, newline: str = "\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.replace("\n", newline).encode("utf-8"))


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "proj"
    _write(r / "pkg" / "mod.py", SRC)
    return r


def test_content_hash_is_stable_across_crlf_lf_and_trailing_whitespace():
    lf = "def f():\n    return 1\n"
    assert evmod.content_hash(lf) == evmod.content_hash(lf.replace("\n", "\r\n"))
    assert evmod.content_hash(lf) == evmod.content_hash("def f():   \n    return 1\t\n")
    assert evmod.content_hash(lf).startswith("sha256:")
    assert evmod.content_hash(lf) != evmod.content_hash(lf.replace("1", "2"))
    assert evmod.content_hash(lf) != evmod.content_hash(lf.replace("    return", "  return"))  # indentation counts


def test_source_evidence_hash_identical_for_crlf_and_lf_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a / "m.py", SRC, "\n")
    _write(b / "m.py", SRC, "\r\n")
    ea = evmod.source_evidence(a, "m.py", 3, 5, commit="c1")
    eb = evmod.source_evidence(b, "m.py", 3, 5, commit="c1")
    assert ea["content_hash"] == eb["content_hash"]
    assert ea["excerpt"] == eb["excerpt"] == "def compute(x):\n    y = x * 2\n    return y + 1"
    assert (ea["locator"], ea["path"], ea["line_start"], ea["line_end"]) == ("m.py:3-5", "m.py", 3, 5)
    assert evmod.source_evidence(a, "m.py", 4, commit=None)["locator"] == "m.py:4"


def test_source_evidence_for_missing_lines_or_file_is_none(repo):
    assert evmod.source_evidence(repo, "pkg/mod.py", 99, commit=None) is None
    assert evmod.source_evidence(repo, "pkg/nope.py", 1, commit=None) is None
    assert evmod.source_evidence(repo, "pkg/mod.py", 0, commit=None) is None
    assert evmod.source_evidence(repo, "pkg/mod.py", 7, 99, commit=None) is None  # range past the end


def test_documents_are_design_docs_and_resolvers_rank_with_tests(repo):
    _write(repo / "docs" / "adr.md", "# Decision\n\nWe keep it simple.\n")
    ev = evmod.source_evidence(repo, "docs/adr.md", 3, commit=None)
    assert ev["source_type"] == "design_doc" and ev["meta"]["reclassified_from"] == "source_code"
    assert ev["meta"]["anchor"]["sec"] == "Decision"
    assert evmod.effective_type({"source_type": "source_code", "path": "README.rst"}) == "design_doc"
    assert evmod.SOURCE_RANK["static_resolution"] == 2 and evmod.is_verifying({"source_type": "static_resolution"})


def test_excerpt_is_truncated(tmp_path):
    _write(tmp_path / "big.py", "\n".join(f"x{i} = {'a' * 40}" for i in range(50)) + "\n")
    ev = evmod.source_evidence(tmp_path, "big.py", 1, 50, commit=None)
    assert len(ev["excerpt"]) <= evmod.EXCERPT_MAX and ev["excerpt"].endswith("...")


def test_check_source_unchanged_and_line_ending_conversion(repo):
    ev = evmod.source_evidence(repo, "pkg/mod.py", 3, 5, commit=None)
    chk = evmod.check_source(repo, ev)
    assert chk.ok and chk.reason == "cited lines unchanged" and chk.current_hash == ev["content_hash"]
    _write(repo / "pkg" / "mod.py", SRC, "\r\n")  # e.g. git autocrlf checkout
    assert evmod.check_source(repo, ev).ok


def test_check_source_detects_changed_lines(repo):
    for anchor in (True, False):
        _write(repo / "pkg" / "mod.py", SRC)
        ev = evmod.source_evidence(repo, "pkg/mod.py", 3, 5, commit=None, anchor=anchor)
        assert ("anchor" in ev["meta"]) is anchor
        _write(repo / "pkg" / "mod.py", SRC.replace("x * 2", "x * 3"))
        chk = evmod.check_source(repo, ev)
        assert not chk.ok and chk.reason == "cited lines changed" and chk.status == "changed"
        assert chk.current_hash and chk.current_hash != ev["content_hash"] and chk.moved_to is None
        # anchored: a candidate location only (the anchored symbol changed), never OK by itself
        assert chk.candidate == ((3, 5) if anchor else None)
        assert chk.as_dict() == {"ok": False, "reason": "cited lines changed", "current_hash": chk.current_hash,
                                 "moved_to": None}
        assert chk.details()["status"] == "changed"


def test_check_source_relocates_a_moved_anchored_block_exactly(repo):
    ev = evmod.source_evidence(repo, "pkg/mod.py", 3, 5, commit=None)
    assert ev["meta"]["anchor"]["sym"] == "compute"
    _write(repo / "pkg" / "mod.py", "# header\n# another\n" + SRC)
    chk = evmod.check_source(repo, ev)
    assert chk.ok and chk.status == "moved" and chk.moved_to == (5, 7)
    assert chk.reason.startswith("cited lines moved to 5-7")
    assert chk.as_dict()["moved_to"] == [5, 7] and chk.details()["status"] == "moved"


def test_unanchored_moved_block_is_reported_but_not_ok(repo):
    ev = evmod.source_evidence(repo, "pkg/mod.py", 3, 5, commit=None, anchor=False)
    _write(repo / "pkg" / "mod.py", "# header\n# another\n" + SRC)
    chk = evmod.check_source(repo, ev)
    assert not chk.ok and chk.moved_to == (5, 7) and chk.status == "moved"
    assert chk.reason == "cited block moved to lines 5-7"


def test_legacy_search_never_relocates_an_ambiguous_block(tmp_path):
    body = "def a():\n    sys.exit(1)\n\n\ndef b():\n    sys.exit(1)\n"
    _write(tmp_path / "m.py", "import sys\n\n" + body)
    ev = evmod.source_evidence(tmp_path, "m.py", 8, commit=None, anchor=False)  # b's sys.exit(1)
    _write(tmp_path / "m.py", "import sys\n# shifted\n\n" + body)
    chk = evmod.check_source(tmp_path, ev)
    assert not chk.ok and chk.status == "ambiguous" and chk.moved_to is None
    assert "occurs 2 times" in chk.reason and "not relocated" in chk.reason


def test_anchor_relocates_a_duplicated_line_to_the_right_occurrence(tmp_path):
    """The audit's wrong relocation: `sys.exit(1)` cited in one function was sent to an identical line elsewhere."""
    body = "def a(x):\n    if x:\n        sys.exit(1)\n    return x\n\n\ndef b(y):\n    sys.exit(1)\n"
    _write(tmp_path / "m.py", "import sys\n\n" + body)
    ev = evmod.source_evidence(tmp_path, "m.py", 10, commit=None)  # b's sys.exit(1)
    assert ev["meta"]["anchor"]["sym"] == "b"
    _write(tmp_path / "m.py", "import sys\n# one\n# two\n\n" + body.replace("def b(y):\n", "def b(y):\n    # why\n"))
    chk = evmod.check_source(tmp_path, ev)
    assert chk.ok and chk.moved_to == (13, 13), chk
    lines = (tmp_path / "m.py").read_text(encoding="utf-8").splitlines()
    assert lines[12].strip() == "sys.exit(1)" and lines[10].startswith("def b")
    # a's identical line moved too, and is told apart from b's
    ev_a = evmod.source_evidence(tmp_path, "m.py", 7, commit=None)
    assert ev_a["meta"]["anchor"]["sym"] == "a" and evmod.check_source(tmp_path, ev_a).status == "same"


def test_check_source_detects_deleted_symbol_file_and_truncation(repo):
    ev = evmod.source_evidence(repo, "pkg/mod.py", 7, 8, commit=None)
    _write(repo / "pkg" / "mod.py", "import os\n")  # the cited function is gone
    chk = evmod.check_source(repo, ev)
    assert not chk.ok and chk.status == "gone" and "other no longer exists" in chk.reason
    legacy = evmod.source_evidence(repo, "pkg/mod.py", 1, commit=None, anchor=False)
    _write(repo / "pkg" / "mod.py", "")
    assert evmod.check_source(repo, legacy).reason == "cited lines changed"
    (repo / "pkg" / "mod.py").unlink()
    chk = evmod.check_source(repo, ev)
    assert not chk.ok and chk.reason == "file no longer exists: pkg/mod.py" and chk.status == "gone"


def test_relocations_are_appended_to_evidence_locations(tmp_path, repo):
    st = Store(tmp_path / "atlas.db")
    try:
        ev = evmod.source_evidence(repo, "pkg/mod.py", 3, 5, commit=None)
        eid = evmod.add(st, ev)
        row = st.evidence(eid)
        assert not evmod.record_location(st, row, evmod.check_source(repo, row))  # unchanged: nothing written
        _write(repo / "pkg" / "mod.py", "# header\n" + SRC)
        chk = evmod.check_source(repo, row)
        assert evmod.record_location(st, row, chk, "snp_1")
        assert not evmod.record_location(st, row, chk, "snp_1")  # same location twice: one row
        _write(repo / "pkg" / "mod.py", SRC)
        assert evmod.record_location(st, row, evmod.check_source(repo, row))  # back where it was: recorded
        locs = st.evidence_locations(eid)
        assert [(x["status"], x["line_start"]) for x in locs] == [("moved", 4), ("same", 3)]
        assert st.evidence(eid)["line_start"] == 3  # the evidence row is immutable
        s = evmod.summarize(row, location=locs[0], grade="full")
        assert s["now"] == "pkg/mod.py:4-6 (moved)" and s["grade"] == "full"
    finally:
        st.close()


def test_check_source_uses_meta_root_for_external_checkouts(tmp_path, repo):
    ext = tmp_path / "reference-checkout"
    _write(ext / "lib" / "ref.py", "def upstream():\n    return 42\n")
    ev = evmod.source_evidence(ext, "lib/ref.py", 1, 2, commit="abc", source_type="reference_repo",
                               meta={"root": str(ext)})
    # Re-checked against the checkout, not the analysed repo (which has no lib/ref.py).
    assert evmod.check_source(repo, ev).ok
    _write(ext / "lib" / "ref.py", "def upstream():\n    return 43\n")
    assert not evmod.check_source(repo, ev).ok


def test_check_source_rejects_non_file_evidence(repo):
    ev = {"source_type": "official_doc", "locator": "https://example.org", "path": None, "line_start": None}
    assert evmod.check_source(repo, ev).reason == "not a file/line evidence"


def test_graph_edge_evidence_is_recorded_but_never_verifying():
    ev = evmod.graph_edge_evidence({"source": "a", "target": "b", "relation": "calls", "confidence": "INFERRED",
                                    "confidence_score": 0.7, "source_file": "x.py", "source_location": "L12"},
                                   graph_path="g.json", commit="c")
    assert ev["source_type"] == "graph_edge" and ev["line_start"] == ev["line_end"] == 12
    assert ev["meta"]["confidence"] == "INFERRED" and ev["content_hash"] is None
    assert not evmod.is_verifying(ev)
    for t in ("graph_edge", "user_feedback", "search_result", "model_summary", "secondary"):
        assert not evmod.is_verifying({"source_type": t})
    for t in ("source_code", "test_result", "experiment", "design_doc", "official_doc"):
        assert evmod.is_verifying({"source_type": t})
    assert evmod.SOURCE_RANK["source_code"] < evmod.SOURCE_RANK["design_doc"] < evmod.SOURCE_RANK["graph_edge"]
    # Pointers (not even inference support) are a subset of the non-verifying types; a graph edge is not one.
    assert evmod.NOT_SUPPORT == {"search_result", "model_summary", "user_feedback"}
    assert evmod.NOT_SUPPORT < evmod.NON_VERIFYING and "graph_edge" not in evmod.NOT_SUPPORT


def test_add_validates_source_type_and_summarize_is_small(tmp_path, repo):
    st = Store(tmp_path / "atlas.db")
    try:
        with pytest.raises(ValueError, match="unknown evidence source_type"):
            evmod.add(st, {"source_type": "vibes", "locator": "?"})
        ev = evmod.source_evidence(repo, "pkg/mod.py", 1, 8, commit="0123456789abcdef")
        eid = evmod.add(st, ev)
        row = st.evidence(eid)
        s = evmod.summarize(row)
        assert s["id"] == eid and s["type"] == "source_code" and s["at"] == "pkg/mod.py:1-8"
        assert s["commit"] == "0123456789ab" and len(s["hash"]) == 19 and len(s["excerpt"]) <= 200
    finally:
        st.close()


def test_url_evidence_hashes_the_text():
    ev = evmod.url_evidence("https://docs.example.org/x", "  Some text\r\nmore  ", source_type="official_doc",
                            version="v1")
    assert ev["content_hash"] == evmod.content_hash("  Some text\nmore")
    assert ev["excerpt"] == "Some text\r\nmore" and ev["url"] == ev["locator"]
