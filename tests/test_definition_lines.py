"""The line a definition is cited at: the name's line in every language (a Java method under `@Override` as a
decorated Python def), its span still covering the annotations; and a Python `@overload` group is one node
at the implementation, the stubs kept as its `overloads` metadata."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import networkx as nx  # noqa: E402

from verinoda import anchors, index, review_rules  # noqa: E402

JAVA = """package a;

@Deprecated
public class Reader {
  @SuppressWarnings("fallthrough")
  int doPeek() {
    return 1;
  }

  @Override
  public
  String toString() { return ""; }

  int plain() { return 2; }
}
"""

KOTLIN = """@Suppress("x")
class K {
  @JvmStatic
  fun foo(): Int = 1

  @Deprecated("x") fun bar() {}
}
"""

CSHARP = """[Serializable]
public class C {
  [HttpGet]
  public int Foo() { return 1; }
}
"""

TS = """@Component({})
class T {
  @Input()
  foo(): number { return 1; }

  @HostListener('x') bar() {}
}
"""

PY = """from typing import overload
import typing


@overload
def Field(a: int) -> int: ...


@overload
def Field(a: str) -> str: ...


def Field(a):
    return helper(a)


def helper(a):
    return a


class Box:
    @typing.overload
    def get(self, k: int) -> int: ...

    @typing.overload
    def get(self, k: str) -> str: ...

    def get(self, k):
        return helper(k)

    @property
    def size(self):
        return 1
"""

PYI = """from typing import overload

@overload
def only(a: int) -> int: ...
@overload
def only(a: str) -> str: ...
"""


def _extract(tmp_path: Path, files: dict[str, str]) -> dict:
    from verinoda.project_index.extract import extract

    paths = []
    for name, body in files.items():
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        paths.append(p)
    return extract(paths, cache_root=tmp_path / "cache", root=tmp_path, parallel=False)


def _lines(out: dict, file: str) -> dict[str, list[int]]:
    got: dict[str, list[int]] = {}
    for n in out["nodes"]:
        if Path(str(n.get("source_file") or "")).name == file and str(n.get("source_location", "")).startswith("L"):
            label = str(n["label"]).strip("()").lstrip(".")
            got.setdefault(label, []).append(int(n["source_location"][1:]))
    return got


def test_annotated_declarations_are_cited_at_their_name_line(tmp_path):
    out = _extract(tmp_path, {"Reader.java": JAVA, "K.kt": KOTLIN, "C.cs": CSHARP, "t.ts": TS})
    java = _lines(out, "Reader.java")
    assert java["doPeek"] == [6]       # not the @SuppressWarnings line (5)
    assert java["toString"] == [12]    # the name's line, below `@Override` and a lone `public`
    assert java["plain"] == [14]
    assert java["Reader"] == [4]       # a class under an annotation too
    kt = _lines(out, "K.kt")
    assert kt["foo"] == [4] and kt["bar"] == [6] and kt["K"] == [2]
    cs = _lines(out, "C.cs")
    assert cs["Foo"] == [4] and cs["C"] == [2]
    ts = _lines(out, "t.ts")
    assert ts["foo"] == [4] and ts["bar"] == [6] and ts["T"] == [2]


def test_python_overloads_are_one_node_at_the_implementation(tmp_path):
    out = _extract(tmp_path, {"models.py": PY, "stubs.py": PYI})
    py = _lines(out, "models.py")
    assert py["Field"] == [13]          # the def without @overload, not the first stub (6)
    assert py["get"] == [28]
    assert py["size"] == [32]
    by_label = {(Path(n["source_file"]).name, str(n["label"]).strip("()").lstrip(".")): n
                for n in out["nodes"] if n.get("source_file")}
    field = by_label[("models.py", "Field")]
    assert field["metadata"]["overloads"] == [6, 10]
    assert "overload_stub" not in field["metadata"]
    assert by_label[("models.py", "get")]["metadata"]["overloads"] == [23, 26]
    assert "metadata" not in by_label[("models.py", "helper")] or \
        "overloads" not in by_label[("models.py", "helper")]["metadata"]
    # the implementation's body is the node's: its call reaches helper
    calls = {(e["source"], e["target"]) for e in out["edges"] if e["relation"] == "calls"}
    assert (field["id"], by_label[("models.py", "helper")]["id"]) in calls
    # stubs only (a protocol or stub module): the node stays at the first stub and says so
    only = by_label[("stubs.py", "only")]
    assert only["source_location"] == "L4"
    assert only["metadata"]["overloads"] == [4, 6] and only["metadata"]["overload_stub"] is True


def test_the_span_starts_at_the_annotation_and_is_found_from_the_name_line(tmp_path):
    (tmp_path / "Reader.java").write_text(JAVA, encoding="utf-8")
    ends, heads = index.ts_def_info(JAVA.encode(), ".java")
    assert heads[6] == 5 and ends[6] == ends[5] == 8
    assert heads[4] == 3 and 14 not in heads  # an unannotated method has no head
    G = nx.MultiDiGraph()
    G.add_node("f", label="Reader.java", file_type="code", source_file="Reader.java", source_location="L1")
    G.add_node("new", label=".doPeek()", file_type="code", source_file="Reader.java", source_location="L6")
    G.add_node("old", label=".toString()", file_type="code", source_file="Reader.java", source_location="L10")
    G.add_node("plain", label=".plain()", file_type="code", source_file="Reader.java", source_location="L14")
    g = index.Graph(G=G, path=tmp_path / "graph.json", root=tmp_path)
    assert g.span("new") == (5, 8) and g.span_basis("new") == "tree-sitter"
    assert g.span("old") == (10, 12)      # a graph made before (cited at the annotation): the same span
    assert g.span("plain") == (14, 14)
    assert g.symbol_at("Reader.java", 5) == "new"  # the annotation line belongs to the method


def test_anchor_facts_give_the_name_line_as_def_and_review_rules_find_both(tmp_path):
    facts = anchors.compute_facts("Reader.java", JAVA.encode())
    peek = next(s for q, s in facts["symbols"].items() if q.endswith("doPeek"))
    assert (peek["start"], peek["def"], peek["end"]) == (5, 6, 8)
    tree = index._ts_parser(".java").parse(JAVA.encode())
    assert review_rules.ts_param_count(tree, 6) == (0, 0, False)
    assert review_rules.ts_param_count(tree, 5) == (0, 0, False)  # facts cached before: the first line
    assert anchors.cache_scheme(anchors.TS_SCHEME) != anchors.TS_SCHEME
    assert anchors.cache_scheme(anchors.PY_SCHEME) == anchors.PY_SCHEME
