"""What an answer leaves out (docs/DESIGN.md D57): context the critique refuted, a mechanism read as a setting."""

from __future__ import annotations

from verinoda import question_plan as qp
from verinoda.analysis_view import render_text


def test_how_does_x_decide_is_a_mechanism_what_decides_x_a_setting():
    assert [c["intent"] for c in qp.clause_cues("how does the renderer decide which passages fit the budget?")] \
        == ["flow"]
    assert [c["intent"] for c in qp.clause_cues("what decides the retry timeout?")] == ["config"]
    assert [c["intent"] for c in qp.clause_cues("which setting controls the cache size?")] == ["config"]


def test_context_the_critique_refuted_is_counted_not_printed():
    res = {
        "analysis_id": "ana_x", "snapshot": {"id": "snp_x", "commit": "abc"}, "status": "answered",
        "subquestions": [{"id": "q1", "status": "met", "intent": "locate", "text": "where is f?",
                          "answer_claim_ids": ["c1"]}],
        "claims": [
            {"id": "c1", "text": "`f` is defined at a.py:1-2", "status": "statically_verified"},
            {"id": "c2", "text": "`g()` in b.py reads environment variable X (b.py:9)", "status": "contradicted"},
            {"id": "c3", "text": "`h()` calls `f()` (c.py:4)", "status": "strong_inference"},
        ],
        "critique": [{"claim": "c2", "before": "strong_inference", "after": "contradicted",
                      "fails": ["b.py:9 has no environment read"]}],
        "unknowns": [],
    }
    text = render_text(res)
    assert "`h()` calls `f()`" in text  # other context stays
    assert "reads environment variable X" not in text and "has no environment read" not in text
    assert "+1 context claim(s) the critique refuted" in text


def test_a_module_qualified_name_links_to_the_function_of_that_module(tmp_path):
    """`naming.resolve` names `resolve` in naming.py: linked by its name, not by identifier parts to whatever
    else is called `resolve` (it used to be 'no symbol in the index is named `naming.resolve`')."""
    import os

    os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")
    from verinoda import index, search_index, workflow
    from verinoda.store import open_store

    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "naming.py").write_text("def resolve(text):\n    return text\n", encoding="utf-8")
    (root / "pkg" / "other.py").write_text("def resolve(x):\n    return x\n", encoding="utf-8")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    search_index._HANDLES.clear()
    g = index.load(root)
    ix = qp._index(g)
    hits = qp._match_string(g, ix, "naming.resolve")
    assert [(g.file(n), tier) for n, _s, tier in hits if tier == "qualified"] == [("pkg/naming.py", "qualified")]
    hits = qp._match_string(g, ix, "pkg.naming.resolve")
    assert any(g.file(n) == "pkg/naming.py" and tier == "qualified" for n, _s, tier in hits)
