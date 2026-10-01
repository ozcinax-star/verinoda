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
    ends, named = index.ts_def_info(JAVA.encode(), ".java")
    assert named[6] == [("doPeek", 5, 8, True)] and ends[5] == 8 and 6 not in ends  # ends: first lines only
    assert named[4] == [("Reader", 3, 15, False)] and named[14] == [("plain", 14, 14, True)]
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


ONE_LINE_JAVA = """package a;

@Deprecated
public enum Color { RED, GREEN }
@FunctionalInterface
public interface Fn { void apply(int x); }
@Deprecated
public class W { void a() { b(); }
  void b() {}
}
"""

ONE_LINE_CS = """[Obsolete]
public interface IX { void M(); }
"""


def _graph(tmp_path: Path, out: dict) -> index.Graph:
    G = nx.MultiDiGraph()
    for n in out["nodes"]:
        G.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    return index.Graph(G=G, path=tmp_path / "graph.json", root=tmp_path)


def test_a_member_on_an_annotated_types_name_line_keeps_its_own_span(tmp_path):
    out = _extract(tmp_path, {"E.java": ONE_LINE_JAVA, "F.cs": ONE_LINE_CS})
    g = _graph(tmp_path, out)
    ids = {(Path(n["source_file"]).name, str(n["label"]).strip("()").lstrip(".")): n["id"]
           for n in out["nodes"] if n.get("source_file")}
    assert g.span(ids[("E.java", "Fn")]) == (5, 6)       # the type: from its annotation
    assert g.span(ids[("E.java", "apply")]) == (6, 6)    # the member on its name line: its own line only
    assert g.span(ids[("E.java", "W")]) == (7, 10)
    assert g.span(ids[("E.java", "a")]) == (8, 8)        # not the class's end (10)
    assert g.span(ids[("E.java", "Color")]) == (3, 4)
    if ("E.java", "RED") in ids:                          # an enum constant is not the enum
        assert g.span(ids[("E.java", "RED")])[0] == 4
    assert g.span(ids[("F.cs", "IX")]) == (1, 2)
    assert g.span(ids[("F.cs", "M")]) == (2, 2)
    # the annotation line belongs to the type, not to the member declared on the type's name line
    assert g.symbol_at("E.java", 5) == ids[("E.java", "Fn")]
    assert g.symbol_at("E.java", 7) == ids[("E.java", "W")]
    assert g.symbol_at("F.cs", 1) == ids[("F.cs", "IX")]


def test_review_rules_pick_the_member_not_the_annotated_type_on_its_name_line():
    tree = index._ts_parser(".java").parse(ONE_LINE_JAVA.encode())
    assert review_rules.ts_param_names(tree, 6) == ["x"]          # the method, not the interface
    assert review_rules.ts_header(tree, 6) is None                # the method has no body
    assert review_rules.ts_header(tree, 6, "Fn").startswith("@ FunctionalInterface public interface Fn")
    assert review_rules.ts_header(tree, 5) is not None            # facts made before: the type's first line
    assert review_rules.ts_header(tree, 8, "W").endswith("class W")
    assert review_rules.ts_header(tree, 8, "a") == "void a ( )"
    assert review_rules.ts_header(tree, 8) == "void a ( )"         # no name: the innermost exact match


def test_a_decorated_ts_class_without_export_moves_to_its_name_line(tmp_path):
    src = "export {};\n\n\n\n\n\n\n\n\n@Injectable()\nclass U {}\n\n@Injectable()\nexport class V {}\n"
    out = _extract(tmp_path, {"t2.ts": src})
    ts = _lines(out, "t2.ts")
    assert ts["U"] == [11]   # the decorator is inside class_declaration: before, U was cited at 10
    assert ts["V"] == [14]   # the decorator is in export_statement: the class line, as before


def test_review_rules_do_not_take_the_class_keyword_for_the_class():
    tree = index._ts_parser(".java").parse(b"@Deprecated\nclass Q {\n  int f() { return 1; }\n}\n")
    assert review_rules._ts_def_at(tree, 2).type == "class_declaration"  # the token `class` also starts there
    assert review_rules.ts_header(tree, 2) == "@ Deprecated class Q"
