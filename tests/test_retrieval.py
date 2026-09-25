"""Retrieval (bounded, justified) and trace (directed, located hops) on a scanned orders_app copy."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index, retrieval, search_index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def g(tmp_path_factory):
    repo = tmp_path_factory.mktemp("retrieval") / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()
    return index.load(repo)


# -- retrieve -----------------------------------------------------------------------------------

def test_items_are_justified_and_located(g):
    res = retrieval.retrieve(g, "where is the order saved to the database?")
    assert res["terms"] and res["items"]
    for it in res["items"]:
        assert it["why"] and it["file"] and it["lines"] and it["lines"][0] <= it["lines"][1]
        assert (g.root / it["file"]).is_file()
        if it["excerpt"]:
            src = (g.root / it["file"]).read_text(encoding="utf-8").splitlines()
            assert it["excerpt"].splitlines()[0] == src[it["lines"][0] - 1]
    assert any(i["file"] == "orders/repository.py" for i in res["items"])
    json.dumps(res)


def test_budget_truncation_is_respected(g):
    for max_chars in (300, 800, 2000, 6000):
        b = retrieval.Budget(max_items=40, max_chars=max_chars)
        res = retrieval.retrieve(g, "order total discount repository save place_order", b)
        assert res["budget"]["used_chars"] <= max_chars
        assert res["budget"]["truncated"] is True
        # The budget covers what is actually returned (items, metadata, edges), not just excerpts.
        assert len(json.dumps(res, ensure_ascii=False)) <= max_chars
    few = retrieval.retrieve(g, "order total discount repository save", retrieval.Budget(max_items=2))
    assert len(few["items"]) == 2 and few["budget"]["truncated"] is True
    roomy = retrieval.retrieve(g, "apply_discount", retrieval.Budget(max_items=50, max_chars=100_000))
    assert roomy["budget"]["truncated"] is False


def test_module_level_constants_are_retrievable(g):
    res = retrieval.retrieve(g, "DISCOUNT_THRESHOLD")
    mod = [i for i in res["items"] if i["symbol"] == "(module level)"]
    hit = next(i for i in mod if i["file"] == "orders/config.py")
    assert hit["lines"] == [7, 7] and "ORDERS_DISCOUNT_THRESHOLD" in hit["excerpt"]
    assert any("orders/config.py:7" in w for w in hit["why"])


def test_tests_can_be_excluded(g):
    res = retrieval.retrieve(g, "apply_discount threshold", include_tests=False)
    assert res["items"] and not any(i["file"].startswith("tests/") for i in res["items"])


def test_edges_between_items_carry_confidence_and_origin(g):
    res = retrieval.retrieve(g, "place_order save repository", retrieval.Budget(max_items=12, max_chars=20_000))
    save = [e for e in res["edges"] if e["from"] == "place_order()" and e["to"] == ".save()"]
    assert save and save[0]["confidence"] == "INFERRED" and save[0]["derived_by"] == index.RECEIVER_ORIGIN
    assert all(e["at"] for e in res["edges"])


# -- trace --------------------------------------------------------------------------------------

def test_trace_flow_handler_to_repository_save(g):
    res = retrieval.trace(g, "create_order_handler", "OrderRepository.save")
    assert res["status"] == "found" and res["mode"] == "flow" and res["direction"] == "directed"
    assert res["resolved"]["target"]["at"] == "orders/repository.py:15"
    hops = res["paths"][0]
    assert [h["from"] for h in hops] + [hops[-1]["to"]] == ["create_order_handler()", "place_order()", ".save()"]
    assert [h["at"] for h in hops] == ["orders/api.py:18", "orders/service.py:22"]
    assert all(h["relation"] == "calls" for h in hops)
    assert hops[0]["confidence"] == "EXTRACTED" and "derived_by" not in hops[0]
    assert hops[1]["confidence"] == "INFERRED" and hops[1]["derived_by"] == index.RECEIVER_ORIGIN


def test_trace_flow_only_follows_calls_and_init(g):
    # Construction runs __init__: that is the one class->method step flow mode takes.
    res = retrieval.trace(g, "get_repo", "orders/repository.py::__init__")
    assert res["status"] == "found"
    assert [(h["relation"], h["to"]) for h in res["paths"][0]] == [("calls", "OrderRepository"),
                                                                    ("method", ".__init__()")]
    # save() is a method of OrderRepository but constructing it does not *call* save.
    none = retrieval.trace(g, "get_repo", "OrderRepository.save")
    assert none["status"] == "no directed path" and none["paths"] == []
    anymode = retrieval.trace(g, "get_repo", "OrderRepository.save", mode="any")
    assert anymode["status"] == "found" and anymode["paths"][0][-1]["relation"] == "method"


def test_trace_direction_is_respected(g):
    res = retrieval.trace(g, "OrderRepository.save", "create_order_handler")
    assert res["status"] == "no directed path"
    assert res["undirected_hint"][0] == ".save()" and res["undirected_hint"][-1] == "create_order_handler()"


def test_trace_unresolved_endpoints(g):
    res = retrieval.trace(g, "definitely_not_a_symbol_xyz", "place_order")
    assert res["status"] == "unresolved" and res["resolved"]["source"] is None and res["paths"] == []
    assert res["resolved"]["target"]["at"] == "orders/service.py:19"
    res2 = retrieval.trace(g, "place_order", "zzzqqq")
    assert res2["status"] == "unresolved" and res2["resolved"]["target"] is None


def test_trace_never_replaces_a_name_written_as_code(g):
    # a code-shaped endpoint with no such symbol is not traced to a similar one
    res = retrieval.trace(g, "create_order_handler", "place_orders")
    assert res["status"] == "unresolved" and res["resolved"]["target"] is None and res["paths"] == []
    assert res["not_found"]["target"] == ("no symbol named `place_orders` in this repository; nearest: place_order "
                                          "(orders/service.py:19)")
    assert "fuzzy" not in res and "not_found" not in retrieval.trace(g, "create_order_handler", "place_order")
    # `Owner.name` needs that owner: another class's `save` is not `Foo.save`
    foo = retrieval.trace(g, "create_order_handler", "Foo.save")
    assert foo["status"] == "unresolved" and foo["not_found"]["target"].startswith("no symbol named `Foo.save`")
    assert "not_found" not in retrieval.trace(g, "api.create_order_handler", "OrderRepository.save")
    # exact forms are exact: ids, path::symbol, Class.method, a file
    for s, t in (("orders_api_create_order_handler", "orders/repository.py::save"),
                 ("create_order_handler()", "OrderRepository.save")):
        exact = retrieval.trace(g, s, t)
        assert exact["status"] == "found" and "not_found" not in exact and "fuzzy" not in exact, (s, t)
    # plain words may still resolve by similarity, and the result says so
    words = retrieval.trace(g, "create order handler", "OrderRepository.save")
    assert words["status"] == "found" and words["fuzzy"]["source"].startswith(
        "'create order handler' has no exact match; resolved by similarity to create_order_handler()")


@pytest.mark.parametrize("source, target, mode", [
    ("orders\\api.py", "orders\\repository.py", "any"),  # Windows paths
    ("create_order_handler", "orders/repository.py::OrderRepository.save", "flow"),  # trace's own hint form
    ("create_order_handler()", "OrderRepository.save()", "any"),
    ("orders.api", "orders.repository", "any"),  # dotted module names
    ("orders/api", "orders/repository", "any"),  # paths without the extension
    ("api.create_order_handler", "orders.repository.OrderRepository.save", "flow"),  # module-qualified
    ("create_order_handler", "OrderRepository#save", "flow"),  # Java-style member
])
def test_trace_resolves_every_form_of_an_existing_name(g, source, target, mode):
    res = retrieval.trace(g, source, target, mode=mode)
    assert "not_found" not in res and "fuzzy" not in res, res.get("not_found") or res.get("fuzzy")
    assert res["status"] == "found" and res["paths"], (source, target, res["status"])


def test_names_exactly_reads_java_packages_and_members(tmp_path):
    import networkx as nx

    G = nx.MultiDiGraph()
    f = "src/main/java/com/example/glowmod/entity/Wisp.java"
    G.add_node("wisp", label="Wisp", source_file=f, file_type="code", source_location="L5")
    G.add_node("spawn", label=".spawn()", source_file=f, file_type="code", source_location="L9")
    G.add_edge("wisp", "spawn", relation="method")
    gr = index.Graph(G=G, path=tmp_path / "graph.json", root=tmp_path)
    for text, node in (("com.example.glowmod.entity.Wisp", "wisp"), ("Wisp#spawn", "spawn"),
                       ("com.example.glowmod.entity.Wisp.spawn", "spawn"), ("entity.Wisp", "wisp")):
        assert retrieval._names_exactly(gr, text, node), text
    for text, node in (("com.example.other.Wisp", "wisp"), ("Spirit#spawn", "spawn")):
        assert not retrieval._names_exactly(gr, text, node), text


def test_trace_says_which_file_lacks_a_file_scoped_name(g):
    res = retrieval.trace(g, "create_order_handler", "orders/service.py::place_orders")
    assert res["status"] == "unresolved" and res["not_found"]["target"].startswith(
        "no symbol named `place_orders` in orders/service.py")
    res = retrieval.trace(g, "create_order_handler", "orders/servce.py::place_order")
    assert res["status"] == "unresolved"


@pytest.mark.parametrize("source, target, side, where", [
    ("create_order_handler", "config.DISCOUNT_THRESHOLD", "target",
     "`DISCOUNT_THRESHOLD` occurs at orders/config.py:7"),
    ("OrderRepository.__init__", "OrderRepository.conn", "target", "`conn` occurs at orders/repository.py:10"),
    ("service.compute_total", "apply_discount", "source", "`compute_total` occurs at orders/service.py:4"),
    ("api.place_order", "validate_items", "source", "`place_order` occurs at orders/api.py:4"),
])
def test_trace_keeps_members_constants_and_imported_names_that_exist(g, source, target, side, where):
    # review round 2: trace said "no symbol named ..." for a module constant, an instance attribute and
    # a name a module imports; it now checks existence as analyze does (the owner's own lines first)
    res = retrieval.trace(g, source, target, mode="any")
    assert "not_found" not in res, res.get("not_found")
    assert res["status"] != "unresolved" and where in res["fuzzy"][side]


def test_trace_does_not_find_what_a_class_does_not_define(g):
    # `self.conn.execute(` inside OrderRepository is sqlite's method, not a member of the class
    res = retrieval.trace(g, "create_order_handler", "OrderRepository.execute")
    assert res["status"] == "unresolved"
    assert res["not_found"]["target"].startswith("no symbol named `OrderRepository.execute` in this repository")


def test_trace_same_endpoint_is_ambiguous(g):
    assert retrieval.trace(g, "place_order", "orders/service.py::place_order")["status"].startswith("ambiguous")


def test_resolve_forms(g):
    assert g.resolve("orders/service.py::place_order")[0] == "orders_service_place_order"
    assert g.resolve("OrderRepository.save")[0] == "orders_repository_orderrepository_save"
    assert g.label(g.resolve("orders/pricing.py")[0]) == "pricing.py"
    assert g.resolve("orders_service_place_order")[0] == "orders_service_place_order"
    nid, cands = g.resolve("definitely_not_a_symbol_xyz")
    assert nid is None  # fuzzy scorer hits are not accepted without a real token match


# -- scoring on a large codebase shape (regression: one huge function used to win every question) ----

def _big_repo(root: Path) -> Path:
    """A 1,200-line dispatcher that mentions every word somewhere and calls 20 helpers,
    next to a short function that actually renders and cuts output at a token budget."""
    (root / "app").mkdir(parents=True)
    (root / "docs").mkdir()
    body = ['def dispatch(argv):', '    """Dispatch every command of the tool."""']
    words = ["token", "budget", "output", "render", "cut", "fit"]
    for i in range(1, 1200):
        if i % 190 == 0:
            body.append(f"    {words[(i // 190) % len(words)]}_setting_{i} = {i}  # unrelated use")
        elif i == 600:
            body.append("    flux_capacitor_quantize = argv  # the only flux capacitor in the project")
        elif 700 <= i < 720:
            body.append(f"    helper_{i - 700:03d}()")
        else:
            body.append(f"    x{i} = {i}")
    body.append("    return 0")
    (root / "app" / "cli.py").write_text(
        "from app.helpers import *  # noqa\n\n\n" + "\n".join(body) + "\n", encoding="utf-8")
    (root / "app" / "helpers.py").write_text(
        "".join(f"def helper_{i:03d}():\n    return {i}\n\n\n" for i in range(20)), encoding="utf-8")
    (root / "app" / "render.py").write_text(
        "def render_text(nodes, token_budget):\n"
        '    """Render nodes as text and cut the output to fit the token budget."""\n'
        "    output = '\n'.join(nodes)\n"
        "    if len(output) > token_budget * 3:\n"
        "        output = output[: token_budget * 3]  # cut to fit\n"
        "    return output\n", encoding="utf-8")
    fold = "def fold_budget(items, budget):\n    total = sum(items)\n    return min(total, budget)\n"
    (root / "app" / "fold_a.py").write_text(fold, encoding="utf-8")
    (root / "app" / "fold_b.py").write_text(fold, encoding="utf-8")
    for i in range(6):
        (root / "docs" / f"guide{i}.md").write_text(
            f"# Guide {i}\n\nThe renderer cuts output to fit the token budget (variant {i}).\n", encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    repo = _big_repo(tmp_path_factory.mktemp("bigret") / "big")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()
    return index.load(repo)


BIG_Q = "Where is the rendered output cut to fit the token budget?"


def test_huge_function_does_not_outrank_the_focused_one(big):
    res = retrieval.retrieve(big, BIG_Q, retrieval.Budget(max_items=10, max_chars=20_000))
    syms = [i["symbol"] for i in res["items"]]
    assert syms[0] == "render_text()", syms
    disp = next((i for i in res["items"] if i["symbol"] == "dispatch()"), None)
    assert disp is None or disp["score"] < res["items"][0]["score"] / 2
    # the dispatcher calls 20 helpers: graph proximity alone never outranks a text match
    # and is labelled as such (it adds at most PPR_LAMBDA to a score)
    for it in res["items"]:
        if not any(w.startswith(("text:", "name:", "question names")) for w in it["why"]):
            assert it["why"][0].startswith("graph") and it["score"] <= search_index.PPR_LAMBDA + 1e-9


def test_items_carry_full_span_and_an_excerpt_window_on_the_matched_lines(big):
    res = retrieval.retrieve(big, "Which function holds the flux capacitor?",
                             retrieval.Budget(max_items=5, max_chars=20_000))
    disp = next(i for i in res["items"] if i["symbol"] == "dispatch()")
    start, end = big.span(disp["id"])
    assert disp["span"] == [start, end] and end - start > 1000
    a, b = disp["lines"]
    assert start <= a <= b <= end and b - a + 1 <= retrieval.Budget().excerpt_lines
    src = (big.root / "app" / "cli.py").read_text(encoding="utf-8").splitlines()
    assert disp["excerpt"].splitlines() == src[a - 1:b]
    assert "flux_capacitor_quantize" in disp["excerpt"]      # the matched line, not the first 14 lines
    assert a > start + 100
    for it in res["items"]:
        if it["id"] in big.G and it["span"]:
            assert tuple(it["span"]) == big.span(it["id"])
            assert it["span"][0] <= it["lines"][0] <= it["lines"][1] <= it["span"][1]


def test_distinct_rare_terms_beat_repeated_common_ones(big):
    """idf is counted per unit, and a unit scores its best passage: the dispatcher mentions
    (nearly) every word, but scattered over 1,200 lines, so no passage of it competes."""
    rk = search_index.rank(big, BIG_Q, ppr=False)
    render = next(h for h in rk.hits if h.name == "render_text")
    disp = next(h for h in rk.hits if h.name == "dispatch")
    assert render.lex == 1.0 and disp.lex < 0.5
    assert len(disp.passages[0]) == 4 and len(render.body_terms) > len(disp.body_terms)
    conn = sqlite3.connect(search_index.db_path_for(big))
    try:
        df = dict(conn.execute("SELECT term, df FROM df WHERE term IN ('budget', 'render')").fetchall())
    finally:
        conn.close()
    assert df["budget"] > df["render"]  # "budget" also occurs in fold_a.py and fold_b.py


def test_question_naming_an_identifier_ranks_it_first(big):
    res = retrieval.retrieve(big, "What is affected if helper_007 changes?")
    first = res["items"][0]
    assert first["symbol"] == "helper_007()" and "question names 'helper_007'" in first["why"]


def test_identical_text_is_folded_and_prose_is_capped(big):
    res = retrieval.retrieve(big, "fold the budget", retrieval.Budget(max_items=10, max_chars=20_000))
    folds = [i for i in res["items"] if i["symbol"] == "fold_budget()"]
    assert len(folds) == 1 and folds[0]["same_text_at"][0].startswith("app/fold_")
    prose = retrieval.retrieve(big, "renderer cuts output variant", retrieval.Budget(max_items=10, max_chars=20_000))
    md = [i for i in prose["items"] if i["file"].endswith(".md")]
    assert len(md) == retrieval.MAX_PROSE_ITEMS and prose["budget"]["prose_items_skipped"] >= 1
    assert all(i["span"] == [1, 3] for i in md)  # a heading owns its section
    assert len(json.dumps(prose, ensure_ascii=False)) <= 20_000


def test_term_matching_respects_identifier_boundaries_and_stems():
    rx = retrieval.term_regex(retrieval.stem("budgets"))
    assert rx.search("token_budget = 3") and rx.search("tokenBudget") and rx.search("Budget")
    assert not retrieval.term_regex("cut").search("execute()")
    assert retrieval.stem("environment") == "environ" and retrieval.stem("variables") == "variabl"
    assert retrieval.stem("_query_terms") == "_query_terms"  # identifiers are kept verbatim
    assert retrieval.named_identifiers("Is OrderRepository.save or compute_total() used in graph.json?") == \
        ["OrderRepository", "save", "compute_total"]


# -- JSON shape, tiers and the "more" list ------------------------------------------------------

def test_items_keep_their_shape_and_top_items_carry_a_call_outline(g):
    res = retrieval.retrieve(g, "What is the call path from the create-order handler to the database write?",
                             retrieval.Budget(max_items=10, max_chars=20_000))
    base = {"id", "symbol", "file", "lines", "span", "score", "why", "excerpt"}
    for it in res["items"]:
        assert base <= set(it) <= base | {"calls", "called_by", "same_text_at"}
    handler = next(i for i in res["items"] if i["symbol"] == "create_order_handler()")
    assert "place_order@18" in handler["calls"]
    first = retrieval.retrieve(g, "What does place_order call?")["items"][0]
    assert first["symbol"] == "place_order()"
    # the receiver-pass edge place_order -> save is INFERRED: marked '?'
    assert "save@22?" in first["calls"] and "validate_items@20" in first["calls"]
    assert "create_order_handler (orders/api.py:18)" in first["called_by"]
    assert any(c.startswith("test_empty_order_rejected (tests/test_service.py:") for c in first["called_by"])
    calls, callers, n_calls, n_callers = retrieval.call_outline(g, first["id"], impact=True)
    assert n_calls == len(calls) == 3 and n_callers == len(callers) == 3
    handler = next(c for c in callers if c.startswith("create_order_handler"))
    assert " <- " not in handler  # impact adds the callers' callers; the handler has none


def test_lower_ranked_items_get_shorter_excerpts_and_the_rest_is_listed(g):
    res = retrieval.retrieve(g, "order", retrieval.Budget(max_items=8, max_chars=20_000))
    items = res["items"]
    assert len(items) == 8 and res["budget"]["truncated"]
    for n, it in enumerate(items):
        size = it["lines"][1] - it["lines"][0] + 1
        cap = 14 if n < retrieval.JSON_FULL_ITEMS else (7 if n < retrieval.JSON_EXCERPT_ITEMS else retrieval.JSON_TAIL_LINES)
        assert size <= cap, (n, it["lines"])
    more = res["budget"]["more"]
    assert set(res) == {"question", "terms", "items", "edges", "budget"}
    assert more and all(":" in m and "-" in m.split(" ")[0] for m in more)
    shown = {f"{i['file']}:{i['span'][0]}" for i in items}
    assert not any(m.split(" ")[0].rsplit("-", 1)[0] in shown for m in more)


def test_seeds_enter_with_full_lexical_score_and_their_reason(g):
    nid = g.resolve("orders/pricing.py::apply_discount")[0]
    res = retrieval.retrieve(g, "zzqx nothing matches this", seeds={nid: "mention m1 linked (exact_label)"})
    first = res["items"][0]
    assert first["id"] == nid and "plan: mention m1 linked (exact_label)" in first["why"]
    # a seed that is not in the graph is ignored, never invented
    res2 = retrieval.retrieve(g, "zzqx nothing matches this", seeds={"no_such_node": "x"})
    assert all(i["id"] != "no_such_node" for i in res2["items"])


def test_caller_expansions_are_searched_and_reported(g):
    q = "Siparişin veritabanına yazıldığı yer nerede?"
    plain = retrieval.retrieve(g, q)
    assert "nerede" not in plain["terms"] and "siparisin" in plain["terms"]  # Turkish stopword dropped, folded
    res = retrieval.retrieve(g, q, expansions={"sipariş": ["order"], "veritabanı": ["database", "save"]})
    db = search_index.tokens("database")[0]
    assert any(e.startswith(f"veritabanı->{db} (question plan)") for e in res["budget"]["expansions"])
    top = [i["symbol"] for i in res["items"][:6]]
    assert "(module level)" in top or ".save()" in top or "OrderRepository" in top


def test_turkish_normalisation_and_apostrophe_suffixes(g):
    res = retrieval.retrieve(g, "pricing.py'deki compute_total'ı nerede? İndirim eşiği nedir?")
    assert "compute_total" in res["terms"] and "indirim" in res["terms"]
    assert not {"deki", "ı", "ndirim", "nerede", "nedir"} & set(res["terms"])
    assert res["items"][0]["symbol"] == "compute_total()"


def test_include_tests_false_also_filters_the_more_list(g):
    res = retrieval.retrieve(g, "order total discount test", retrieval.Budget(max_items=3, max_chars=20_000),
                             include_tests=False)
    assert not any(i["file"].startswith("tests/") for i in res["items"])
    assert not any(m.startswith("tests/") for m in res["budget"].get("more", []))


# -- render_text ---------------------------------------------------------------------------------

def test_render_text_is_skeleton_first_with_outlines_and_constants(g):
    res = retrieval.retrieve(g, "How does create_order_handler reach the database write?")
    text = retrieval.render_text(res, budget_chars=6000)
    lines = text.splitlines()
    assert not any(ln.startswith("# ") for ln in lines)  # the question is not echoed
    heads = [ln for ln in lines if ln.startswith("## ")]
    # the passage starts at the signature: the header names the function, the signature is printed once
    assert heads and heads[0] == "## orders/api.py:16-21 create_order_handler"
    assert text.count("def create_order_handler(payload: dict) -> tuple[int, dict]") == 1
    assert "  calls: place_order@18, get_repo@18" in text or "  calls: get_repo@18, place_order@18" in text
    assert "save@22?" in text  # flow question: the outline of place_order, INFERRED marked
    assert "'?' = inferred edge" in text
    assert len(text) <= 6000


def test_render_text_dedents_each_window_and_keeps_the_locators(g):
    res = retrieval.retrieve(g, "How does create_order_handler reach the database write?")
    text = retrieval.render_text(res, budget_chars=6000)
    # a method's passage starts at column 0; its place is in the header, not in the indentation
    assert "## orders/repository.py:9-13 OrderRepository.__init__\ndef __init__(self, url: str = DATABASE_URL):" \
        in text
    assert "    self.conn = sqlite3.connect(url)" in text  # relative indentation inside the window stays
    for ln in text.splitlines():
        if ln.startswith("## "):
            assert re.match(r"## \S+:\d+-\d+ \S", ln), ln


@pytest.mark.parametrize("q", ["create_order_handler", "How does create_order_handler reach the database write?",
                               "order"])
def test_render_text_never_loses_a_signature_to_the_short_header(g, q):
    """Whatever the budget cuts (the passage, or the note at the end pushing it out), an item whose
    header names it without its signature still has the signature printed."""
    res = retrieval.retrieve(g, q)
    sigs = {f"{h.file}:{h.a}-{h.b}": h.sig for h in res.render.ranking.hits if h.sig}
    for n in range(150, 3200, 23):
        text = retrieval.render_text(res, budget_chars=n)
        assert len(text) <= n
        flat = " ".join(text.split())  # a data file's "signature" is its first lines joined
        for m in re.finditer(r"^## (\S+:\d+-\d+) ", text, re.M):
            sig = sigs.get(m.group(1))
            if sig:
                assert " ".join(sig.split())[:25] in flat, (n, m.group(0), sig)


def test_render_text_states_truncation_with_the_follow_up_command(g):
    res = retrieval.retrieve(g, "order")
    text = retrieval.render_text(res, budget_chars=700)
    assert len(text) <= 700
    assert "more candidates not shown" in text and "next: same query, --max-chars 1400" in text


def test_render_text_lists_at_most_three_expansions_without_their_notes(g):
    res = retrieval.retrieve(g, "order")
    res["budget"]["expansions"] = ["a->b (abbreviation)", "c->d (turkish stem 'c')", "e->f (gloss)",
                                   "g->h (gloss)", "i->j (gloss)"]
    first = retrieval.render_text(res, budget_chars=3000).splitlines()[0]
    assert first == "expanded: a->b, c->d, e->f (+2 more)"


def test_render_text_says_so_when_nothing_matches(g):
    res = retrieval.retrieve(g, "zzqxv_nothing_like_this")
    text = retrieval.render_text(res)
    assert text.strip() and "## " not in text


def test_render_text_shows_module_constants_the_body_references(g):
    res = retrieval.retrieve(g, "load_settings")
    text = retrieval.render_text(res)
    assert "  const orders/config.py:5: DATABASE_URL = " in text


def test_render_text_after_a_json_round_trip_uses_the_items(g):
    res = retrieval.retrieve(g, "where is the order saved to the database?")
    plain = json.loads(json.dumps(res))
    text = retrieval.render_text(plain, budget_chars=3000)
    assert "## orders/" in text and len(text) <= 3000 and "# where is" not in text
    assert retrieval.render_text(res) != text  # the ranking attached to the live result gives more


def test_retrieve_result_serialises_and_compares_like_a_dict(g):
    res = retrieval.retrieve(g, "apply_discount")
    again = retrieval.retrieve(g, "apply_discount")
    assert res == again == json.loads(json.dumps(res))
    assert isinstance(res, dict) and res.render is not None


# -- trace: containment is not reachability; unresolved endpoints get hints ---------------------------

def test_trace_any_mode_reports_containment_hops_as_structural(g):
    res = retrieval.trace(g, "get_repo", "OrderRepository.save", mode="any")
    assert res["status"] == "found" and res["reachability"] == "structural"
    kinds = [h["kind"] for h in res["paths"][0]]
    assert kinds[-1] == "containment" and "not that execution reaches the target" in res["note"]
    flow = retrieval.trace(g, "create_order_handler", "OrderRepository.save")
    assert all(h["kind"] == "call" for h in flow["paths"][0]) and "reachability" not in flow
    init = retrieval.trace(g, "get_repo", "orders/repository.py::__init__")
    assert [h["kind"] for h in init["paths"][0]] == ["call", "construction"]


def test_trace_unresolved_endpoint_returns_hints_and_a_next_step(g):
    res = retrieval.trace(g, "applyDiscountRate", "place_order")
    assert res["status"] == "unresolved" and res["resolved"]["source"] is None
    assert "source" in res["hints"] and "target" not in res["hints"]
    assert res["next_step"].startswith("pass one of the hints")
    hints = res["hints"]["source"]
    assert hints and hints[0]["label"] == "apply_discount()" and hints[0]["at"].startswith("orders/pricing.py:")
    assert all(set(h) == {"id", "label", "at"} for h in hints)
