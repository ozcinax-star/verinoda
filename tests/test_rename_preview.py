"""Rename preview (verinoda/rename_preview.py): every line a rename of one symbol would touch, each with its status
and the line; the mentions nothing ties to it; other symbols of the same name; conflicts with the new name; and
nothing written."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import hashlib  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, index, rename_preview, search_index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

FILES = {
    "shop/__init__.py": "",
    "shop/pricing.py": """TAX = 2


def compute_total(items):
    \"\"\"compute_total sums items.\"\"\"
    return sum(items) + TAX


class Order:
    def total(self):
        return compute_total([1])

    def report(self):
        return self.total()
""",
    "shop/checkout.py": """from shop.pricing import compute_total


def pay(cart):
    price_total = 0
    a = compute_total(cart)
    return a + compute_total(cart) + price_total
""",
    "shop/alias.py": """from shop.pricing import compute_total as ct


def run():
    return ct([2])
""",
    "shop/other.py": """def compute_total_v2():
    return 'compute_total'
""",
    "shop/legacy.py": """def compute_total(rows):
    return len(rows)


def count(rows):
    return compute_total(rows)
""",
    "README.md": "# Shop\n\nCall `compute_total` to price a cart.\n",
    "src/main/java/p/Shape.java": """package p;

public class Shape {
    public int area() { return 0; }
}
""",
    "src/main/java/p/Square.java": """package p;

public class Square extends Shape {
    public int area() { return 4; }
    public int size() { return 2; }
    public int twice() { return area() * 2; }
}
""",
    "src/main/java/p/Circle.java": """package p;

public class Circle extends Shape {
    public int area() { return 3; }
}
""",
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("rename") / "shop"
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


def _run(repo: Path, symbol: str, new: str) -> dict:
    res = rename_preview.run(index.load(repo), symbol, new)
    assert res["status"] == "found", res
    return res


def _at(rows: list[dict]) -> dict[str, dict]:
    return {f"{r['at']}:{r['column']}": r for r in rows}


def test_python_function_sites_with_their_status(repo):
    res = _run(repo, "shop/pricing.py::compute_total", "price_items")
    sites = _at(res["sites"])
    assert res["old"] == "compute_total" and res["new"] == "price_items"
    assert res["sites"][0]["kind"] == "definition" and res["sites"][0]["at"] == "shop/pricing.py:4"
    assert sites["shop/pricing.py:4:5"]["status"] == "statically_verified"
    # an import from its module and a call graded by the call-site check
    assert sites["shop/checkout.py:1:26"]["kind"] == "import"
    assert sites["shop/checkout.py:1:26"]["status"] == "statically_verified"
    assert sites["shop/checkout.py:6:9"] == {**sites["shop/checkout.py:6:9"], "kind": "call",
                                              "status": "statically_verified"}
    # the second call of pay has no edge of its own: bound by the caller (and the import), strong_inference
    second = sites["shop/checkout.py:7:16"]
    assert second["kind"] == "bound" and second["status"] == "strong_inference"
    # its own module (the docstring, the method's call) and the aliased import line
    assert sites["shop/pricing.py:11:16"]["kind"] == "call"
    assert sites["shop/pricing.py:5:8"]["kind"] == "bound"
    assert sites["shop/alias.py:1:26"]["kind"] == "import"
    # the alias call does not spell the name: not a site, said why
    assert any(x["at"] == "shop/alias.py:5" and "alias" in x["why"] for x in res["not_spelled"])
    assert "shop/alias.py:5:12" not in sites
    assert all(s["line"] and s["why"] for s in res["sites"])


def test_mentions_other_symbols_and_conflicts(repo):
    res = _run(repo, "shop/pricing.py::compute_total", "price_total")
    ments = _at(res["mentions"])
    # a string in another file spells it; nothing ties it to the symbol
    assert ments["shop/other.py:2:13"]["status"] == "weak_inference"
    assert "shop/other.py:1:5" not in ments  # compute_total_v2 is another word
    # legacy.py defines its own compute_total: left alone, with the call the index ties to it
    assert [o["at"] for o in res["other_symbols"]] == ["shop/legacy.py:1"]
    assert all(r["at"] not in ("shop/legacy.py:1", "shop/legacy.py:6") for r in res["sites"] + res["mentions"])
    assert any(r["at"] == "shop/legacy.py:6" for r in res["other_symbol_lines"])
    # checkout.py already has a local price_total, and the rename edits that file
    assert any(c["at"] == "shop/checkout.py:5" and c["status"] == "weak_inference" for c in res["conflicts"])
    # a module that already defines the new name
    same = _run(repo, "shop/pricing.py::compute_total", "Order")
    assert any(c["at"] == "shop/pricing.py:9" and c["status"] == "statically_verified" for c in same["conflicts"])


def test_java_method_overrides_are_renamed_with_it(repo):
    res = _run(repo, "Square.area", "surface")
    fam = {f["label"]: f["why"] for f in res["family"]}
    assert "Shape.area" in fam and "overrides" in fam["Shape.area"]
    assert "Circle.area" in fam and "same base method" in fam["Circle.area"]
    kinds = {r["at"]: (r["kind"], r["status"]) for r in res["sites"]}
    assert kinds["src/main/java/p/Square.java:4"][0] == "definition"
    assert kinds["src/main/java/p/Shape.java:4"] == ("override", "strong_inference")
    assert kinds["src/main/java/p/Circle.java:4"] == ("override", "strong_inference")
    # a Java call line is inference at most: which definition it binds is checked for Python only
    assert kinds["src/main/java/p/Square.java:6"][1] != "statically_verified"
    assert not res["other_symbols"]
    # a sibling member of the same name is a conflict
    clash = _run(repo, "Square.area", "size")
    assert any(c["at"] == "src/main/java/p/Square.java:5" for c in clash["conflicts"])


@pytest.mark.parametrize("new, why", [("price total", "not an identifier"), ("class", "reserved word"),
                                      ("compute_total", "current name"), ("1st", "not an identifier")])
def test_a_new_name_that_cannot_be_used_is_refused(repo, new, why):
    res = rename_preview.run(index.load(repo), "shop/pricing.py::compute_total", new)
    assert res["status"] == "invalid" and why in res["note"]


def test_names_that_do_not_resolve(repo):
    g = index.load(repo)
    assert rename_preview.run(g, "nothing_like_this_here", "x")["status"] in ("not_found", "ambiguous")
    amb = rename_preview.run(g, "compute_total", "x")
    assert amb["status"] == "ambiguous" and len(amb["candidates"]) == 2
    assert rename_preview.run(g, "shop/pricing.py", "x")["status"] == "not_a_symbol"


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and ".verinoda" not in p.relative_to(root).parts}


def _claims(root: Path) -> int:
    st = open_store(root)
    try:
        return st.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    finally:
        st.close()


def test_cli_lists_the_sites_and_writes_nothing(repo, capsys):
    before = _tree(repo)
    claims = _claims(repo)
    assert cli.main(["rename-preview", "shop/pricing.py::compute_total", "price_items", "--repo", str(repo),
                     "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "found" and out["counts"]["sites"] == len(out["sites"]) and out["counts"]["files"] >= 3
    assert out["written"].startswith("nothing")
    assert cli.main(["rename-preview", "shop/pricing.py::compute_total", "price_items", "--repo", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "shop/checkout.py:6:9  call [statically_verified]" in text and "mention" in text
    # a conflict is exit 3, an unresolved name or a bad new name exit 2
    assert cli.main(["rename-preview", "shop/pricing.py::compute_total", "price_total", "--repo", str(repo)]) == 3
    assert cli.main(["rename-preview", "compute_total", "x", "--repo", str(repo)]) == 2
    assert cli.main(["rename-preview", "shop/pricing.py::compute_total", "a b", "--repo", str(repo)]) == 2
    capsys.readouterr()
    assert _tree(repo) == before
    assert _claims(repo) == claims


def test_a_changed_file_keeps_inference_at_most(repo, tmp_path):
    import shutil

    copy = tmp_path / "shop"
    shutil.copytree(repo, copy)
    p = copy / "shop" / "checkout.py"
    p.write_bytes(p.read_bytes() + b"\n# edited\n")
    res = rename_preview.run(index.load(copy), "shop/pricing.py::compute_total", "price_items",
                             stale=["shop/checkout.py"])
    rows = [r for r in res["sites"] if r["at"].startswith("shop/checkout.py")]
    assert rows and all(r["status"] != "statically_verified" and "changed since the index" in r["why"] for r in rows)



def test_a_line_only_a_guessed_edge_ties_to_it_is_a_mention(repo):
    g = index.load(repo)
    target = next(n for n in g.G.nodes if g.file(n) == "shop/pricing.py" and g.label(n).startswith("compute_total"))
    src = next(n for n in g.G.nodes if g.file(n) == "shop/other.py" and g.label(n).startswith("compute_total_v2"))
    g.G.add_edge(src, target, relation="indirect_call", confidence="INFERRED", source_file="shop/other.py",
                 source_location="L2")
    res = rename_preview.run(g, "shop/pricing.py::compute_total", "price_items")
    assert all(s["at"] != "shop/other.py:2" for s in res["sites"])
    guessed = _at(res["mentions"])["shop/other.py:2:13"]
    assert guessed["kind"] == "inferred" and guessed["status"] == "weak_inference" and "INFERRED" in guessed["why"]
    assert "weak_inference" not in res["counts"]["by_status"]
    assert "INFERRED edge" in rename_preview.render(res)
