"""Control and data dependence in Python functions, backward and forward slices, across callers."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import textwrap  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, index, slicing  # noqa: E402


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


def _lines(res: dict) -> dict[int, list[str]]:
    return {int(x["at"].rsplit(":", 1)[1]): x["role"] for x in res["lines"]}


SHOP = """
import sqlite3

RATE = 0.2


def price_of(item, qty, discount=0):
    base = item["price"] * qty
    if qty > 10:
        base = base * 0.9
    total = base - discount
    if total < 0:
        return 0
    taxed = total * (1 + RATE)
    return taxed


def save(db, order_id, amount):
    rows = []
    for i in range(3):
        rows.append(i)
    note = "x"
    db.execute("INSERT INTO t VALUES (?, ?)", (order_id, amount))
    return rows


def checkout(db, cart, user):
    disc = 5 if user.vip else 0
    amount = 0
    for item, qty in cart:
        amount += price_of(item, qty, disc)
    if not cart:
        raise ValueError("empty")
    oid = user.id * 1000
    save(db, oid, amount)
    return amount


def handler(request):
    db = sqlite3.connect("shop.db")
    cart = request.json["cart"]
    checkout(db, cart, request.user)


class Repo:
    def put(self, value, tag="none"):
        return value

    def use(self, x):
        y = x + 1
        return self.put(y)
"""


@pytest.fixture
def shop(tmp_path) -> Path:
    _write(tmp_path, "shop/orders.py", SHOP)
    return tmp_path


# -- inside a function ------------------------------------------------------------------------------

def test_a_return_depends_on_its_definitions_and_the_branches_that_lead_to_it(shop):
    res = slicing.backward(shop, "shop/orders.py", 14, depth=0)
    lines = _lines(res)
    assert set(lines) == {7, 8, 9, 10, 11, 13, 14}
    assert lines[11] == ["control"]   # `if total < 0: return 0` decides whether line 14 runs
    assert lines[8] == ["control"] and lines[9] == ["def"]
    assert 12 not in lines   # the other return
    assert res["free"] == ["RATE"] and res["status"] == "strong_inference"
    assert [p["name"] for p in res["parameters"]] == ["discount", "item", "qty"]


def test_a_list_built_in_a_loop_reaches_its_return(shop):
    lines = _lines(slicing.backward(shop, "shop/orders.py", 23, depth=0))
    assert {18, 19, 20, 23} <= set(lines) and 21 not in lines and 22 not in lines


def test_an_argument_of_a_call_is_sliced_alone(shop):
    res = slicing.backward(shop, "shop/orders.py", 34, arg="2", depth=0)
    lines = _lines(res)
    assert {27, 28, 29, 30} <= set(lines) and 33 not in lines   # amount, not oid
    assert res["criterion"]["of"].startswith("argument 2")
    res = slicing.backward(shop, "shop/orders.py", 34, var="oid", depth=0)
    assert 33 in _lines(res) and 27 not in _lines(res)


def test_a_forward_slice_follows_a_definition(shop):
    res = slicing.forward(shop, "shop/orders.py", 27, var="disc")
    assert set(_lines(res)) == {27, 30, 34, 35}


def test_control_flow_forms(tmp_path):
    _write(tmp_path, "m.py", """
    def f(xs, ctx):
        total = 0
        try:
            v = int(xs[0])
        except ValueError as e:
            v = len(str(e))
        with ctx as c:
            w = c.read()
        while total < 10:
            total += v
            if total > 5:
                break
        match w:
            case {"k": k}:
                z = k
            case _:
                z = 0
        if (n := len(xs)) > 2:
            total += n
        squares = [i * i for i in xs]
        return total + z + len(squares)
    """)
    lines = _lines(slicing.backward(tmp_path, "m.py", 21, depth=0))
    for ln in (2, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20):
        assert ln in lines, ln
    assert "control" in lines[9]   # the loop's test decides how often total grows
    e = _lines(slicing.backward(tmp_path, "m.py", 6, depth=0))
    assert 4 in e   # the handler's value comes from what the try body raised


def test_errors_say_what_is_wrong(shop, tmp_path):
    with pytest.raises(slicing.SliceError, match="not inside a function"):
        slicing.backward(shop, "shop/orders.py", 3)
    with pytest.raises(slicing.SliceError, match="neither read nor defined"):
        slicing.backward(shop, "shop/orders.py", 14, var="nope")
    with pytest.raises(slicing.SliceError, match="has no argument"):
        slicing.backward(shop, "shop/orders.py", 34, arg="7")
    with pytest.raises(slicing.SliceError, match="cannot be read"):
        slicing.backward(shop, "shop/missing.py", 1)
    _write(tmp_path, "bad.py", "def f(:\n")
    with pytest.raises(slicing.SliceError, match="does not parse"):
        slicing.backward(tmp_path, "bad.py", 1)
    with pytest.raises(slicing.SliceError, match="depth"):
        slicing.backward(shop, "shop/orders.py", 14, depth=9)


# -- across callers ---------------------------------------------------------------------------------

@pytest.fixture
def scanned(shop) -> Path:
    from verinoda import workflow
    from verinoda.store import open_store

    workflow.init(shop)
    st = open_store(shop)
    try:
        workflow.scan(st, shop)
    finally:
        st.close()
    return shop


def test_a_parameter_is_followed_into_its_callers(scanned):
    g = index.load(scanned)
    res = slicing.backward(scanned, "shop/orders.py", 22, arg="1", depth=2, graph=g)
    params = {p["name"]: p for p in res["parameters"]}
    (hop,) = params["amount"]["callers"]
    assert hop["at"] == "shop/orders.py:34" and hop["argument"] == "amount"
    assert {27, 28, 29, 30} <= {int(x["at"].rsplit(":", 1)[1]) for x in hop["lines"]}
    deeper = {q["name"]: q for q in hop["parameters"]}
    (up,) = deeper["cart"]["callers"]
    assert up["at"] == "shop/orders.py:41" and up["argument"] == "cart"
    assert any(x["at"] == "shop/orders.py:40" for x in up["lines"])
    assert res["callers_followed"] >= 2
    text = slicing.render(res)
    assert "called at shop/orders.py:34" in text


def test_defaults_keywords_and_methods(scanned):
    g = index.load(scanned)
    res = slicing.backward(scanned, "shop/orders.py", 46, var="value", depth=1, graph=g)
    (hop,) = res["parameters"][0]["callers"]
    assert hop["argument"] == "y" and any(x["at"] == "shop/orders.py:49" for x in hop["lines"])
    res = slicing.backward(scanned, "shop/orders.py", 10, var="discount", depth=1, graph=g)
    (hop,) = res["parameters"][0]["callers"]
    assert hop["argument"] == "disc"
    no_graph = slicing.backward(scanned, "shop/orders.py", 10, var="discount", depth=1)
    assert "no index" in no_graph["parameters"][0]["note"]


def test_the_cli(scanned, capsys):
    assert cli.main(["slice", "shop/orders.py:22", "--arg", "1", "--repo", str(scanned), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["direction"] == "backward" and out["parameters"]
    assert cli.main(["slice", "shop/orders.py:27", "--forward", "--var", "disc", "--repo", str(scanned)]) == 0
    assert "forward slice of `disc`" in capsys.readouterr().out
    assert cli.main(["slice", "shop/orders.py", "--repo", str(scanned)]) == 2
    assert cli.main(["slice", "shop/orders.py:26", "--forward", "--depth", "1", "--repo", str(scanned)]) == 2
    assert cli.main(["slice", "shop/orders.py:3", "--repo", str(scanned)]) == 2
