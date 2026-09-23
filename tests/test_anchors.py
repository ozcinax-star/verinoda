"""Symbol facts: fingerprints that ignore formatting and see every token change; anchors and relocation."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import ast  # noqa: E402
import hashlib  # noqa: E402

import pytest  # noqa: E402

from verinoda import anchors  # noqa: E402
from verinoda.store import Store  # noqa: E402

BASE = '''"""Module doc."""

import os
from pkg.helpers import load as _load

LIMIT = int(os.environ.get("LIMIT", "5"))


@cached
def total(items, factor=2):
    """Sum the items."""
    s = 0
    for i in items:
        s += i * factor

    def inner(x):
        return x + 1

    return inner(s)


class Box:
    size = 3

    def open(self, force=False):
        return _load(self.size)
'''


def facts(text: str, rel: str = "pkg/m.py") -> dict:
    f = anchors.compute_facts(rel, text.encode("utf-8"))
    assert f is not None and anchors.usable(f)
    return f


def fps(f: dict) -> dict:
    return {q: (s["sig"], s["body"], s["doc"], s["full"]) for q, s in f["symbols"].items()}


def test_python_symbols_spans_and_facets():
    f = facts(BASE)
    assert set(f["symbols"]) == {"total", "total.inner", "Box", "Box.open"}
    t = f["symbols"]["total"]
    assert (t["start"], t["def"], t["end"], t["kind"]) == (9, 10, 19, "def")  # decorator line starts it
    assert f["symbols"]["Box"]["kind"] == "class" and f["symbols"]["Box.open"]["end"] == 26
    assert set(f["bindings"]) >= {"os", "_load", "LIMIT", "total", "Box"}
    assert "ASSIGN:LIMIT" in f["module"] and "DOC" in f["module"]
    assert anchors.enclosing(f, 14) == ("sym", "total") and anchors.enclosing(f, 17) == ("sym", "total.inner")
    assert anchors.enclosing(f, 6) == ("mod", "ASSIGN:LIMIT") and anchors.enclosing(f, 23) == ("sym", "Box")


@pytest.mark.parametrize("edit", [
    lambda s: s.replace("    s = 0\n", "    s = 0  # start\n"),                 # comment
    lambda s: s.replace("    s = 0\n", "    s = 0\n\n\n"),                        # blank lines
    lambda s: "# header\n\n" + s,                                                 # shift everything
    lambda s: s.replace("\n", "\r\n"),                                            # CRLF checkout
    lambda s: s.replace("for i in items:", "for  i  in  items :"),                # whitespace inside a line
    lambda s: s.replace("class Box:", "class Box:  # container"),                 # comment on a header
])
def test_formatting_leaves_every_fingerprint_unchanged(edit):
    assert fps(facts(edit(BASE))) == fps(facts(BASE))


def test_moving_a_definition_keeps_its_fingerprints():
    moved = BASE.replace("class Box:", "PLACEHOLDER").split("PLACEHOLDER")
    swapped = moved[0].split("@cached")[0] + "class Box:" + moved[1] + "\n\n@cached" + moved[0].split("@cached")[1]
    a, b = facts(BASE), facts(swapped)
    assert fps(a) == fps(b)
    assert a["symbols"]["total"]["start"] != b["symbols"]["total"]["start"]


@pytest.mark.parametrize("edit,changed", [
    (lambda s: s.replace("s += i * factor", "s += i + factor"), {"total": "body"}),
    (lambda s: s.replace("factor=2", "factor=3"), {"total": "sig"}),               # default argument
    (lambda s: s.replace("@cached", "@lru_cache"), {"total": "sig"}),              # decorator
    (lambda s: s.replace("return x + 1", "return x + 2"), {"total.inner": "body", "total": "body"}),  # Merkle
    (lambda s: s.replace('"""Sum the items."""', '"""Add the items."""'), {"total": "doc"}),
    (lambda s: s.replace("size = 3", "size = 4"), {"Box": "body"}),
    (lambda s: s.replace("force=False", "force=True"), {"Box.open": "sig"}),
])
def test_every_token_change_is_seen_in_the_right_facet(edit, changed):
    a, b = facts(BASE)["symbols"], facts(edit(BASE))["symbols"]
    for q, facet in changed.items():
        assert a[q][facet] != b[q][facet], (q, facet)
        assert a[q]["full"] != b[q]["full"]
    untouched = set(a) - set(changed)
    if "total.inner" not in changed and "total" not in changed:
        untouched.discard("total")
    for q in untouched:
        if q == "Box" and "Box.open" in changed:
            continue
        assert a[q]["body"] == b[q]["body"], q


def test_docstring_edit_changes_only_doc_and_full():
    a = facts(BASE)["symbols"]["total"]
    b = facts(BASE.replace('"""Sum the items."""', '"""Add the items."""'))["symbols"]["total"]
    assert (a["sig"], a["body"]) == (b["sig"], b["body"]) and a["doc"] != b["doc"] and a["full"] != b["full"]


def test_bindings_see_rebinding_and_star_imports():
    a = facts(BASE)["bindings"]
    b = facts(BASE.replace("from pkg.helpers import load as _load", "from pkg.other import load as _load"))["bindings"]
    assert a["_load"] != b["_load"] and a["os"] == b["os"]
    c = facts(BASE + "\ntotal = None\n")["bindings"]
    assert c["total"] != a["total"]
    d = facts("from pkg.x import *\n" + BASE)["bindings"]
    assert all(d[k] != a[k] for k in a) and "*" in d  # a star import may rebind any name
    assert anchors.facet_fp(facts(BASE), "bind:pkg/m.py::missing", "bind") == anchors.ABSENT


def test_serializer_does_not_use_ast_dump(monkeypatch):
    monkeypatch.setattr(ast, "dump", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ast.dump used")))
    assert facts(BASE)["symbols"]["total"]["body"]
    assert anchors.PY_SCHEME.startswith("py1-3.")


def test_syntax_error_and_unsupported_files_have_no_usable_facts():
    bad = anchors.compute_facts("pkg/bad.py", b"def broken(:\n")
    assert bad is not None and not anchors.usable(bad) and bad["error"] == "SyntaxError"
    assert anchors.compute_facts("data/x.csv", b"a,b\n") is None
    assert anchors.facet_fp(bad, "sym:pkg/bad.py::broken", "body") is None
    assert anchors.enclosing(None, 3) is None


def test_markdown_sections():
    md = "Intro text.\n\n# Title\n\nBody.\n\n## Part A\n\nA text.\n\n```\n# not a heading\n```\n\n## Part B\nB.\n"
    f = facts(md, "docs/x.md")
    secs = f["sections"]
    assert list(secs) == ["(preamble)", "Title", "Title/Part A", "Title/Part B"]
    assert (secs["Title/Part A"]["start"], secs["Title/Part A"]["end"]) == (7, 13)
    assert anchors.enclosing(f, 9) == ("sec", "Title/Part A")
    g = facts(md.replace("A text.", "A  text."), "docs/x.md")["sections"]
    assert g["Title/Part A"]["h"] == secs["Title/Part A"]["h"]
    h = facts(md.replace("A text.", "Another text."), "docs/x.md")["sections"]
    assert h["Title/Part A"]["h"] != secs["Title/Part A"]["h"] and h["Title/Part B"]["h"] == secs["Title/Part B"]["h"]


def test_tree_sitter_languages_get_symbol_facts():
    js = ("import {a} from './a';\n\n// helper\nexport function foo(x) {\n  return a(x);\n}\n\n"
          "class K {\n  m() { return 1; }\n}\n")
    f = facts(js, "web/app.js")
    assert {"foo", "K", "K.m"} <= set(f["symbols"])
    assert (f["symbols"]["foo"]["start"], f["symbols"]["foo"]["end"]) == (4, 6)
    same = facts(js.replace("// helper\n", "// a longer helper comment\n\n"), "web/app.js")
    assert all(same["symbols"][q]["full"] == s["full"] for q, s in f["symbols"].items())
    changed = facts(js.replace("return a(x);", "return a(x + 1);"), "web/app.js")
    assert changed["symbols"]["foo"]["body"] != f["symbols"]["foo"]["body"]
    assert changed["symbols"]["K.m"]["full"] == f["symbols"]["K.m"]["full"]
    go = facts("package p\n\nfunc Add(a int) int {\n\treturn a + 1\n}\n", "p/add.go")
    assert "Add" in go["symbols"] and go["symbols"]["Add"]["end"] == 5


def test_facts_are_cached_by_content(tmp_path):
    repo = tmp_path / "r"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_bytes(BASE.encode())
    st = Store(tmp_path / "atlas.db")
    try:
        out = anchors.update_facts(st, repo, ["pkg/m.py", repo / "pkg" / "m.py", "pkg/gone.py", "notes.txt"])
        assert (out["computed"], out["cached"], out["missing"], out["unsupported"]) == (1, 1, 1, 1)
        sha = hashlib.sha256(BASE.encode()).hexdigest()
        cached = st.file_facts(sha, anchors.PY_SCHEME)
        assert cached and set(cached["symbols"]) == {"total", "total.inner", "Box", "Box.open"}
        assert anchors.facts_by_sha(st, "pkg/m.py", sha)["sha256"] == sha
        # a request for a version that is no longer on disk only reads the cache
        assert anchors.facts_for(st, repo, "pkg/m.py", sha256="f" * 64) is None
    finally:
        st.close()


def _anchor(text: str, line: int, end: int | None = None) -> dict:
    a = anchors.make_anchor(facts(text), text, line, end or line)
    assert a is not None
    return a


def test_relocate_same_moved_changed_gone():
    a = _anchor(BASE, 14)  # "s += i * factor" inside total
    h = anchors.text_hash(BASE.splitlines()[13])
    assert a["sym"] == "total" and a["ast_path"]
    same = anchors.relocate_in(a, facts(BASE), BASE, h, 14)
    assert (same.status, same.start) == ("same", 14) and same.ok
    shifted = "# one\n# two\n" + BASE.replace("    s = 0\n", "    s = 0\n    # counting\n")
    moved = anchors.relocate_in(a, facts(shifted), shifted, h, 14)
    assert (moved.status, moved.start, moved.end) == ("moved", 17, 17) and moved.ok
    changed_text = BASE.replace("s += i * factor", "s += i * factor * 2")
    ch = anchors.relocate_in(a, facts(changed_text), changed_text, h, 14)
    assert ch.status == "changed" and not ch.ok and ch.start == 14
    gone_text = BASE.replace("def total(items, factor=2):", "def summed(items, factor=2):")
    assert anchors.relocate_in(a, facts(gone_text), gone_text, h, 14).status == "gone"


def test_relocate_whole_definitions_module_statements_and_sections():
    a = _anchor(BASE, 9, 19)  # the whole of total (decorator to end)
    assert a["sym"] == "total" and a["off"] == 0
    text = "\n\n" + BASE
    r = anchors.relocate_in(a, facts(text), text, anchors.text_hash("\n".join(BASE.splitlines()[8:19])), 9)
    assert (r.status, r.start, r.end) == ("moved", 11, 21)
    m = _anchor(BASE, 6)
    assert m["mod"] == "ASSIGN:LIMIT"
    r = anchors.relocate_in(m, facts(text), text, anchors.text_hash(BASE.splitlines()[5]), 6)
    assert (r.status, r.start) == ("moved", 8)
    md = "# T\n\nfirst\n\n## S\n\nsecond line\n"
    sa = anchors.make_anchor(facts(md, "d.md"), md, 7, 7)
    md2 = "# T\n\nfirst, now longer\n\n## S\n\nsecond line\n"
    r = anchors.relocate_in(sa, facts(md2, "d.md"), md2, anchors.text_hash("second line"), 7)
    assert r.status == "same"


def test_scheme_mismatch_is_never_read_as_a_change():
    a = {**_anchor(BASE, 14), "scheme": "py0-2.7"}
    r = anchors.relocate_in(a, facts(BASE), BASE, None, 14)
    assert r.status == "unanchored" and not r.ok


def test_relocation_time_for_a_large_file(tmp_path):
    body = "".join(f"    x{i} = compute({i})\n" for i in range(3000))
    text = "import os\n\n\ndef big():\n" + body + "    sys.exit(1)\n"
    p = tmp_path / "big.py"
    p.write_bytes(text.encode())
    a = anchors.anchor_for_file(p, "big.py", 3005, 3005)
    p.write_bytes(("# shifted\n" + text).encode())
    import time

    t0 = time.perf_counter()
    r = anchors.relocate_file(p, "big.py", a, anchors.text_hash("    sys.exit(1)"), 3005)
    assert (r.status, r.start) == ("moved", 3006)
    assert time.perf_counter() - t0 < 2.0
