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


# -- review round: each scenario's slice, line for line ---------------------------------------------

def _slice(tmp_path, text: str, line: int, **kw) -> dict:
    _write(tmp_path, "m.py", text)
    return slicing.backward(tmp_path, "m.py", line, depth=0, **kw)


def test_an_exception_carries_the_state_before_the_statement_that_raised(tmp_path):
    res = _slice(tmp_path, """
    def f():
        result = None
        try:
            result = compute()
        except Exception:
            pass
        return result
    """, 7)
    assert set(_lines(res)) == {2, 4, 7}
    res = _slice(tmp_path, """
    def g():
        x = 1
        try:
            x = a()
            x = b()
        except ValueError:
            return x
        return 0
    """, 7)
    assert set(_lines(res)) == {2, 4, 5, 7}
    res = _slice(tmp_path, """
    def h():
        x = 1
        try:
            x = a()
        finally:
            return x
    """, 6)
    assert set(_lines(res)) == {2, 4, 6}


@pytest.mark.skipif(not hasattr(__import__("ast"), "TryStar"), reason="except* needs Python 3.11")
def test_an_exception_group_handler_sees_the_state_before_the_raise(tmp_path):
    res = _slice(tmp_path, """
    def k():
        x = 1
        try:
            x = a()
        except* ValueError:
            pass
        return x
    """, 7)
    assert set(_lines(res)) == {2, 4, 7}


def test_a_way_out_of_a_handler_or_else_runs_finally(tmp_path):
    raise_ = """
    def f(a):
        x = 0
        try:
            a()
        except ValueError:
            x = 1
            raise
        finally:
            log(x)
    """
    assert set(_lines(_slice(tmp_path, raise_, 9))) == {2, 4, 6, 9}
    returns = raise_.replace("            raise\n", "            return x\n") + "        return x\n"
    assert set(_lines(_slice(tmp_path, returns, 9))) == {2, 4, 6, 9}
    res = _slice(tmp_path, """
    def e(a):
        x = 0
        try:
            a()
        except ValueError:
            pass
        else:
            x = 1
            return
        finally:
            log(x)
    """, 11)
    assert set(_lines(res)) == {2, 4, 8, 11}


def test_a_nested_class_body_reads_the_function_s_names(tmp_path):
    res = _slice(tmp_path, """
    def f(v):
        class C:
            x = v
        return C
    """, 4)
    assert set(_lines(res)) == {2, 4}
    assert [p["name"] for p in res["parameters"]] == ["v"] and res["free"] == []


def test_a_nested_function_s_own_names_are_not_reads(tmp_path):
    res = _slice(tmp_path, """
    def outer():
        y = 1
        def inner():
            y = 2
            return y
        return inner
    """, 6)
    assert set(_lines(res)) == {3, 6}
    res = _slice(tmp_path, """
    def outer():
        y = 1
        def inner():
            nonlocal y
            return y
        return inner
    """, 6)
    assert set(_lines(res)) == {2, 3, 6}


def test_a_forward_slice_follows_nested_branches(tmp_path):
    _write(tmp_path, "m.py", """
    def f(a, other):
        x = a > 0
        if x:
            if other:
                y = 1
            return y
        return 0
    """)
    res = slicing.forward(tmp_path, "m.py", 2)
    assert set(_lines(res)) == {2, 3, 4, 5, 6, 7}


def test_arg_takes_the_outermost_call_on_the_line(tmp_path):
    text = """
    def f(db, oid, amount, x):
        save(db, g(oid), amount)
        save(x, g(oid), amount)
    """
    res = _slice(tmp_path, text, 2, arg="2")
    assert set(_lines(res)) == {2} and [p["name"] for p in res["parameters"]] == ["amount"]
    res = _slice(tmp_path, text, 3, arg="0")
    assert [p["name"] for p in res["parameters"]] == ["x"]


def test_a_decorator_or_def_header_line_is_the_definition_in_the_function_around(tmp_path):
    text = """
    def outer(b, y):
        @d.wrap
        def inner(p=y,
                  q=b):
            return p
        return inner
    """
    for line in (2, 3, 4):
        res = _slice(tmp_path, text, line)
        assert res["criterion"]["function"] == "outer"
        assert set(_lines(res)) == {3}
        assert [p["name"] for p in res["parameters"]] == ["b", "y"] and res["free"] == ["d"]
    assert _lines(_slice(tmp_path, text, 5)) == {5: ["criterion"]}   # the nested function's own body


def test_del_ends_a_name_and_changes_a_container(tmp_path):
    res = _slice(tmp_path, """
    def f(d, k):
        del d[k]
        return d
    """, 3)
    assert set(_lines(res)) == {2, 3} and [p["name"] for p in res["parameters"]] == ["d", "k"]
    res = _slice(tmp_path, """
    def g(x, c):
        if c:
            del x
        return x
    """, 4)
    assert set(_lines(res)) == {2, 3, 4} and _lines(res)[3] == ["def"]


def test_var_on_a_tuple_assignment_takes_its_own_part(tmp_path):
    res = _slice(tmp_path, """
    def f(x, y):
        a, b = x, y
        return a
    """, 2, var="a")
    assert [p["name"] for p in res["parameters"]] == ["x"]


def test_var_and_arg_together_are_refused(tmp_path):
    with pytest.raises(slicing.SliceError, match="not both"):
        _slice(tmp_path, "def f(a):\n    g(a)\n", 2, var="a", arg="0")


def test_a_one_line_if_is_listed_once(tmp_path):
    res = _slice(tmp_path, """
    def f(a, b):
        if a: return b
        return 0
    """, 2)
    assert [x["at"] for x in res["lines"]] == ["m.py:2"]
    assert res["lines"][0]["role"] == ["control", "criterion"]


def _big(n: int) -> str:
    """A function of about 3n statements and n names, each defined from the one before, under branches."""
    out = ["def big(a, b):", "    v0 = a"]
    for i in range(1, n + 1):
        out += [f"    v{i} = v{i - 1} + b", f"    if v{i} > {i}:", "        b = b + 1"]
    return "\n".join(out + [f"    return v{n} + b"]) + "\n"


def test_reaching_definitions_scale_to_large_functions():
    import ast
    import time

    func = ast.parse(_big(600)).body[0]
    cfg = slicing.CFG(func)
    t0 = time.perf_counter()
    reach = cfg.reaching()
    assert time.perf_counter() - t0 < 3.0   # 1,800 nodes; solved against program order, 538 nodes took 6 s
    last = next(n.id for n in cfg.nodes if n.kind == "return")
    # every `b = b + 1` reaches the return (the branches after it may all be skipped), and the parameter b
    assert len(reach.get(last, "b")) == 601 and reach.get(last, "v600") == {last + 3}


def test_post_dominators_scale_to_large_functions(tmp_path):
    import ast
    import time

    func = ast.parse(_big(1300)).body[0]   # about 3,900 nodes, under the 4,000 limit
    t0 = time.perf_counter()
    fn = slicing._Fn("big.py", func, [])
    assert time.perf_counter() - t0 < 3.0   # control dependence alone took 26 s before
    assert len(fn.cfg.nodes) > 3900
    _write(tmp_path, "big.py", _big(300))
    t0 = time.perf_counter()
    res = slicing.backward(tmp_path, "big.py", 3 * 300 + 3, depth=0)
    assert time.perf_counter() - t0 < 3.0
    assert len(res["lines"]) == 3 * 300 + 2   # every statement: the return depends on all of them


def test_the_cli_reads_paths_inside_the_project_only(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _write(repo, "sub/m.py", "def f(a):\n    b = a\n    return b\n")
    _write(tmp_path, "out.py", "def f(a):\n    return a\n")
    monkeypatch.chdir(repo / "sub")
    assert cli.main(["slice", "m.py:3", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["criterion"]["at"] == "sub/m.py:3"
    assert cli.main(["slice", "../../out.py:2", "--repo", str(repo)]) == 2
    assert "outside the project" in capsys.readouterr().err
    monkeypatch.chdir(repo)
    assert cli.main(["slice", "../out.py:2", "--repo", str(repo)]) == 2
    assert "outside the project" in capsys.readouterr().err
    assert cli.main(["slice", "sub/m.py:3", "--var", "b", "--arg", "0", "--repo", str(repo)]) == 2
    assert "not both" in capsys.readouterr().err


CALLS = """
class Acct:
    def take(self, amount):
        return amount


def helper(x, flag=False):
    return flag


class Box:
    def __init__(self, v):
        self.v = v


def use(a, raw, opts):
    Acct.take(a, raw)
    helper(raw, **opts)
    return Box(raw)
"""


@pytest.fixture
def calls(tmp_path) -> Path:
    from verinoda import workflow
    from verinoda.store import open_store

    _write(tmp_path, "pkg/calls.py", CALLS)
    workflow.init(tmp_path)
    st = open_store(tmp_path)
    try:
        workflow.scan(st, tmp_path)
    finally:
        st.close()
    return tmp_path


def test_callers_through_a_class_and_spread_keywords(calls):
    g = index.load(calls)
    res = slicing.backward(calls, "pkg/calls.py", 3, depth=1, graph=g)
    (hop,) = res["parameters"][0]["callers"]
    assert hop["at"] == "pkg/calls.py:16" and hop["argument"] == "raw"   # Acct.take(a, raw): self is a
    res = slicing.backward(calls, "pkg/calls.py", 7, depth=1, graph=g)
    (hop,) = res["parameters"][0]["callers"]
    assert "argument" not in hop and "**kwargs" in hop["unknown"]
    res = slicing.backward(calls, "pkg/calls.py", 12, var="v", depth=1, graph=g)
    assert "not followed to __init__" in res["parameters"][0]["note"]
