"""The butterfly view: callers and callees of one symbol, or the types it extends and those that extend it
(verinoda/butterfly.py, `verinoda butterfly`, the `ui` panel's /api/butterfly)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import butterfly, cli, index, search_index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

FILES = {
    "app/shapes.py": '''class Shape:
    def area(self):
        return 0


class Polygon(Shape):
    def sides(self):
        return 0


class Square(Polygon):
    def area(self):
        return 1


class Triangle(Polygon):
    pass


class Oops(Exception):
    pass
''',
    "app/flow.py": '''from app.util import clean, fmt


def main():
    run()


def run():
    data = load()
    return fmt(clean(data))


def load():
    return [1, 2]


def other():
    run()
''',
    "app/util.py": '''def clean(xs):
    return strip(xs)


def strip(xs):
    return xs


def fmt(xs):
    return str(xs)
''',
    "app/more.py": '''def rec(n):
    if n:
        return rec(n - 1)
    return 0


class Box:
    def open(self):
        return self.peek()

    def peek(self):
        return 1


def make():
    b = Box()
    return b.open()


def only_tested():
    return 1
''',
    "tests/test_flow.py": '''from app.flow import run
from app.more import only_tested


def test_run():
    assert run()


def test_only():
    assert only_tested()
''',
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("butterfly") / "proj"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    search_index._HANDLES.clear()
    return root


def _labels(side: dict) -> set[str]:
    return {it["label"].lstrip(".").removesuffix("()") for it in side["items"]}


def _sides(res: dict) -> dict:
    return {s["key"]: s for s in res["sides"]}


def test_callers_and_callees_with_their_lines(repo):
    res = butterfly.run(index.load(repo), "app/flow.py::run", depth=1)
    assert res["status"] == "found" and res["mode"] == "calls"
    s = _sides(res)
    assert list(s) == ["callers", "callees"]
    assert {"main", "other", "test_run"} <= _labels(s["callers"])
    assert {"load", "clean", "fmt"} <= _labels(s["callees"])
    for side in s.values():
        for it in side["items"]:
            assert it["depth"] == 1 and it["via"] == res["id"]
            assert re.match(r".+:\d+$", it["edge_at"] or "")  # the line the link is written on
            assert it["claim"]["status"] in ("strong_inference", "weak_inference")  # never verified
            assert it["claim"]["evidence"] == [it["edge_at"]]
    call = next(it for it in s["callers"]["items"] if it["label"].lstrip(".").startswith("main"))
    assert call["edge_at"] == "app/flow.py:5" and "calls" in call["claim"]["text"]


def test_a_second_level_hangs_from_the_first(repo):
    g = index.load(repo)
    s = _sides(butterfly.run(g, "app/flow.py::run", depth=2))
    strip = next(it for it in s["callees"]["items"] if it["label"].lstrip(".").startswith("strip"))
    assert strip["depth"] == 2 and g.label(strip["via"]).lstrip(".").startswith("clean")
    one = _sides(butterfly.run(g, "app/flow.py::run", depth=1))
    assert "strip" not in _labels(one["callees"])


def test_test_code_can_be_left_out(repo):
    s = _sides(butterfly.run(index.load(repo), "app/flow.py::run", depth=1, tests=False))
    assert "test_run" not in _labels(s["callers"]) and "main" in _labels(s["callers"])


def test_a_class_opens_on_its_inheritance_tree(repo):
    res = butterfly.run(index.load(repo), "Polygon", depth=2)
    assert res["status"] == "found" and res["mode"] == "inherits"
    s = _sides(res)
    assert list(s) == ["supertypes", "subtypes"]
    assert _labels(s["supertypes"]) == {"Shape"}
    assert {"Square", "Triangle"} <= _labels(s["subtypes"])
    sq = next(it for it in s["subtypes"]["items"] if it["label"] == "Square")
    assert sq["claim"]["text"] == "Square extends Polygon" and sq["edge_at"].startswith("app/shapes.py:")
    grand = _sides(butterfly.run(index.load(repo), "Square", depth=2))
    up = {it["label"]: it["depth"] for it in grand["supertypes"]["items"]}
    assert up.get("Polygon") == 1 and up.get("Shape") == 2


def test_a_library_supertype_is_a_leaf(repo):
    s = _sides(butterfly.run(index.load(repo), "Oops", mode="inherits"))
    ext = [it for it in s["supertypes"]["items"] if it["external"]]
    assert [it["label"] for it in ext] == ["Exception"]


def test_an_unknown_name_is_not_replaced_by_a_similar_one(repo, capsys):
    res = butterfly.run(index.load(repo), "app/flow.py::nothing_here")
    assert res["status"] in ("not_found", "ambiguous") and "sides" not in res
    assert cli.main(["butterfly", "app/flow.py::nothing_here", "--repo", str(repo)]) == 2


def test_cli_text_and_json(repo, capsys):
    assert cli.main(["butterfly", "app/flow.py::run", "--repo", str(repo), "--json", "--depth", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "found" and [s["key"] for s in out["sides"]] == ["callers", "callees"]
    assert cli.main(["butterfly", "Polygon", "--repo", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "inherits view" in text and "extends / implements: 1" in text and "Square" in text
    assert "not verified" in text


def test_the_ui_panel_data(repo):
    from verinoda.ui import data as uidata

    a = uidata.Atlas(repo)
    a.ensure()
    g = index.load(repo)
    nid = next(n for n in g.G if g.label(n).lstrip(".").startswith("run") and g.file(n) == "app/flow.py")
    res = a.butterfly(nid, None, 1)
    assert res["title"] and res["kind"] == "function" and res["mode"] == "calls"
    callers = res["sides"][0]["items"]
    assert callers and all(it["title"] and it["via_title"] and it["kind"] for it in callers)
    no_tests = a.butterfly(nid, "calls", 1, tests=False)
    assert all(not it["file"].startswith("tests/") for it in no_tests["sides"][0]["items"])
    file_note = next(n for n in g.G if g.is_file_node(n) and g.file(n) == "app/flow.py")
    with pytest.raises(KeyError):
        a.butterfly(file_note)
    with pytest.raises(ValueError):
        a.butterfly(nid, "sideways")


def test_a_direct_recursive_call_is_listed_once_on_each_side(repo, capsys):
    res = butterfly.run(index.load(repo), "app/more.py::rec")
    for side in res["sides"]:
        assert [(it["id"], it["recursive"], it["edge_at"]) for it in side["items"]] == [
            (res["id"], True, "app/more.py:3")]
    assert cli.main(["butterfly", "app/more.py::rec", "--repo", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "none in the index" not in text and text.count("recursive]") == 2


def test_a_class_without_supertypes_or_subtypes_opens_on_its_callers(repo):
    res = butterfly.run(index.load(repo), "Box")
    assert res["mode"] == "calls"
    assert _labels(_sides(res)["callers"]) == {"make"}
    assert butterfly.run(index.load(repo), "Polygon")["mode"] == "inherits"


def test_an_empty_side_says_what_was_left_out(repo, capsys):
    res = butterfly.run(index.load(repo), "app/more.py::only_tested", tests=False)
    callers = _sides(res)["callers"]
    assert callers["items"] == [] and callers["left_out"] == {"tests": 1}
    text = butterfly.render(res)
    assert "called by: 0 (1 left out: test code)\n    none shown\n" in text  # not "none in the index"
    assert _sides(butterfly.run(index.load(repo), "app/more.py::only_tested"))["callers"]["left_out"] == {}


def test_of_two_call_sites_the_first_line_is_cited(repo):
    g = index.load(repo)
    load = next(n for n in g.G if g.label(n) == "load()")
    strip = next(n for n in g.G if g.label(n) == "strip()")
    for ln in (10, 9):
        g.G.add_edge(load, strip, relation="calls", confidence="EXTRACTED", source_file="app/flow.py",
                     source_location=f"L{ln}")
    callees = _sides(butterfly.butterfly(g, load, mode="calls", depth=1))["callees"]["items"]
    assert [it["edge_at"] for it in callees] == ["app/flow.py:9"]


def test_a_method_is_named_with_its_class(repo):
    res = butterfly.run(index.load(repo), "app/more.py::Box.peek", depth=1)
    assert res["symbol"] == "Box.peek"
    opener = _sides(res)["callers"]["items"][0]
    assert opener["label"] == "Box.open" and opener["claim"]["text"] == "Box.open calls Box.peek"


def test_json_names_the_symbol_as_when_does(repo, capsys):
    assert cli.main(["butterfly", "app/flow.py::run", "--repo", str(repo), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["symbol"] == "run()" and "label" not in out and "query" not in out


def test_a_depth_out_of_range_is_refused(repo):
    for bad in ("0", "5"):
        with pytest.raises(SystemExit, match="--depth"):
            cli.main(["butterfly", "app/flow.py::run", "--repo", str(repo), "--depth", bad])
