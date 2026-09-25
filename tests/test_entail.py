"""Entailment grades: does the evidence state the claim (full), concern it (partial), or neither (none)?"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import entail  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "orders_app"


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    r = tmp_path_factory.mktemp("entail") / "orders_app"
    shutil.copytree(EXAMPLE, r, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    (r / "orders" / "extra.py").write_bytes(
        b"import orders.pricing as pricing_mod\n"
        b"from orders.service import place_order as _place\n"
        b"from orders.pricing import apply_discount\n\n\n"
        b"def via_alias(items):\n"
        b"    return _place(None, 'c', items)\n\n\n"
        b"def via_module(items):\n"
        b"    return pricing_mod.compute_total(items)\n\n\n"
        b"def shadowed(apply_discount):\n"
        b"    return apply_discount(1.0)\n\n\n"
        b"def as_value(values):\n"
        b"    return sorted(values, key=apply_discount)\n\n\n"
        b"def in_text():\n"
        b"    return 'apply_discount'  # apply_discount\n")
    (r / "orders" / "flow.py").write_bytes(
        b"from orders.service import validate_items\n\n\n"
        b"def save_items(items):\n"
        b"    return list(items)\n\n\n"
        b"def handle(items):\n"
        b"    def finish():\n"
        b"        return save_items(items)\n"
        b"    validate_items(items)\n"
        b"    return finish()\n\n\n"
        b"def handle_lambda(items):\n"
        b"    done = lambda: save_items(items)  # noqa: E731\n"
        b"    validate_items(items)\n"
        b"    return done()\n\n\n"
        b"class Cart:\n"
        b"    def add(self, item):\n"
        b"        self._check(item)\n\n"
        b"    def _check(self, item):\n"
        b"        return item\n")
    return r


def src(repo: Path, rel: str, a: int, b: int | None = None, **kw) -> dict:
    ev = evmod.source_evidence(repo, rel, a, b, commit=None, **kw)
    assert ev is not None, (rel, a, b)
    return ev


def rel_grade(repo, at, caller, target, target_file, relation="calls"):
    path, _, ln = at.rpartition(":")
    return entail.assess("relation", repo, {"target_label": target, "at": at, "relation": relation},
                         src(repo, path, int(ln)), text="c",
                         subjects=[f"{path}::{caller}", f"{target_file}::{target}"])


# -- relation ----------------------------------------------------------------------------

def test_relation_full_for_an_ast_call_to_the_target(repo):
    g = rel_grade(repo, "orders/api.py:18", "create_order_handler()", "place_order()", "orders/service.py")
    assert (g.grade, g.code) == ("full", "call")
    g = rel_grade(repo, "orders/pricing.py:8", "compute_total()", "apply_discount()", "orders/pricing.py")
    assert g.grade == "full"


def test_relation_through_import_alias_and_module_alias(repo):
    g = rel_grade(repo, "orders/extra.py:7", "via_alias()", "place_order()", "orders/service.py")
    assert (g.grade, g.code) == ("full", "alias_call") and "_place" in g.reason
    g = rel_grade(repo, "orders/extra.py:11", "via_module()", "compute_total()", "orders/pricing.py")
    assert (g.grade, g.code) == ("full", "module_call")
    g = rel_grade(repo, "orders/extra.py:11", "via_module()", "compute_total()", "orders/other.py")
    assert (g.grade, g.code) == ("none", "wrong_module")


def test_relation_none_for_lines_that_do_not_show_the_call(repo):
    g = rel_grade(repo, "orders/api.py:18", "create_order_handler()", "fetch_order()", "orders/service.py")
    assert (g.grade, g.code) == ("none", "absent")
    g = rel_grade(repo, "orders/pricing.py:8", "apply_discount()", "compute_total()", "orders/pricing.py")
    assert g.grade == "none"  # reversed direction: the line is not in apply_discount, nor calls compute_total
    g = rel_grade(repo, "orders/pricing.py:8", "apply_discount()", "apply_discount()", "orders/pricing.py")
    assert (g.grade, g.code) == ("none", "outside_caller")
    g = rel_grade(repo, "orders/extra.py:23", "in_text()", "apply_discount()", "orders/pricing.py")
    assert (g.grade, g.code) == ("none", "string_only")


def test_relation_partial_when_the_callee_is_not_resolved(repo):
    g = rel_grade(repo, "orders/service.py:22", "place_order()", ".save()", "orders/repository.py")
    assert (g.grade, g.code) == ("partial", "method_unresolved")
    g = rel_grade(repo, "orders/extra.py:15", "shadowed()", "apply_discount()", "orders/pricing.py")
    assert (g.grade, g.code) == ("partial", "rebound")
    g = rel_grade(repo, "orders/extra.py:19", "as_value()", "apply_discount()", "orders/pricing.py")
    assert (g.grade, g.code) == ("partial", "not_called")


def test_relation_other_evidence_types(repo):
    spec = {"source": "a_node", "target": "b_node", "target_label": "place_order()", "at": "orders/api.py:18"}
    subjects = ["orders/api.py::create_order_handler()", "orders/service.py::place_order()"]
    edge = evmod.graph_edge_evidence({"source": "a_node", "target": "b_node", "relation": "calls",
                                      "source_file": "orders/api.py", "source_location": "L18"},
                                     graph_path="g", commit=None)
    assert entail.grade("relation", repo, spec, edge, subjects=subjects) == "partial"
    other = {**edge, "meta": {**edge["meta"], "target": "c_node"}, "locator": "a_node -[calls]-> c_node"}
    assert entail.grade("relation", repo, spec, other, subjects=subjects) == "none"
    res = {"source_type": "static_resolution", "locator": "orders/api.py:18", "path": "orders/api.py",
           "line_start": 18, "meta": {"tool": "jedi", "kind": "definitive", "target_path": "orders/service.py"}}
    assert entail.grade("relation", repo, spec, res, subjects=subjects) == "full"
    assert entail.grade("relation", repo, spec, {**res, "meta": {**res["meta"], "kind": "ambiguous"}},
                        subjects=subjects) == "partial"
    assert entail.grade("relation", repo, spec, {**res, "meta": {**res["meta"], "kind": "unresolved"}},
                        subjects=subjects) == "none"
    trace = {"source_type": "experiment", "locator": "run r: orders/api.py:18 -> place_order",
             "meta": {"kind": "call_trace", "outcome": "pass", "callee_qual": "place_order",
                      "callee_path": "orders/service.py"}}
    assert entail.grade("relation", repo, spec, trace, subjects=subjects) == "full"
    doubles = {**trace, "meta": {**trace["meta"], "flags": {"test_double": True}}}
    assert entail.grade("relation", repo, spec, doubles, subjects=subjects) == "partial"


def test_uses_relations_need_a_reference_and_quotes_are_verbatim(repo):
    g = rel_grade(repo, "orders/service.py:25", "fetch_order()", "OrderRepository", "orders/repository.py",
                  relation="uses")
    assert (g.grade, g.code) == ("full", "reference")
    g = rel_grade(repo, "orders/service.py:25", "fetch_order()", "ValidationError", "orders/service.py",
                  relation="uses")
    assert (g.grade, g.code) == ("none", "absent")
    line = src(repo, "orders/repository.py", 1)
    quoted = 'orders/repository.py:1 contains: """Persistence layer: the only module that talks to SQLite."""'
    assert entail.grade("location", repo, {}, line, text=quoted) == "full"  # "only" is quoted, not claimed
    assert entail.grade("location", repo, {}, line, text=quoted.replace("SQLite", "Postgres")) != "full"


def test_inherits_and_imports_relations(repo):
    g = rel_grade(repo, "orders/service.py:8", "ValidationError", "ValueError", "builtins.py", relation="inherits")
    assert g.grade == "full"
    g = rel_grade(repo, "orders/api.py:3", "api.py", "OrderRepository", "orders/repository.py", relation="imports_from")
    assert g.grade == "full"


# -- location / config / exclusive / flow ------------------------------------------------------

def test_location_needs_the_exact_ast_span(repo):
    ok = src(repo, "orders/pricing.py", 6, 8, meta={"symbol": "compute_total()"})
    text = "`compute_total()` is defined at orders/pricing.py:6-8"
    assert entail.grade("location", repo, {}, ok, text=text) == "full"
    long = src(repo, "orders/pricing.py", 6, 9, meta={"symbol": "compute_total()"})
    g = entail.assess("location", repo, {}, long, text="`compute_total()` is defined at orders/pricing.py:6-9")
    assert g.grade == "partial" and "ends at 8" in g.reason
    method = src(repo, "orders/repository.py", 15, 20, meta={"symbol": ".save()"})
    assert entail.grade("location", repo, {}, method, text="`.save()` is defined at x") == "full"
    wrong = src(repo, "orders/pricing.py", 11, 15, meta={"symbol": "compute_total()"})
    assert entail.grade("location", repo, {}, wrong, text="x") == "partial"  # exists, but elsewhere
    sec = src(repo, "docs/adr/0001-sqlite-persistence.md", 1, 7,
              meta={"symbol": "ADR 0001: SQLite for order persistence"})
    assert entail.grade("location", repo, {}, sec, text="x") == "full"


def test_config_needs_an_environment_read_of_the_literal(repo):
    line = src(repo, "orders/config.py", 7)
    assert entail.grade("config", repo, {"env": "ORDERS_DISCOUNT_THRESHOLD"}, line) == "full"
    assert entail.grade("config", repo, {"env": "ORDERS_MAX_ITEMS"}, line) == "none"
    (repo / "orders" / "envdoc.py").write_bytes(b'NAME = "ORDERS_REGION"  # documented only\n')
    assert entail.grade("config", repo, {"env": "ORDERS_REGION"}, src(repo, "orders/envdoc.py", 1)) == "partial"


def test_exclusive_and_flow(repo):
    assert entail.grade("exclusive", repo, {"pattern": r"sqlite3\.connect"}, src(repo, "orders/repository.py", 10)) \
        == "full"
    assert entail.grade("exclusive", repo, {"pattern": r"sqlite3\.connect"}, src(repo, "orders/repository.py", 9)) \
        == "none"
    spec = {"hops": [{"from": "create_order_handler()", "to": "place_order()", "at": "orders/api.py:18",
                      "relation": "calls"}], "sink_kinds": ["sql-write"]}
    hop = src(repo, "orders/api.py", 18, meta={"hop": "create_order_handler()->place_order()"})
    assert entail.grade("flow", repo, spec, hop) == "full"
    sink = src(repo, "orders/repository.py", 17, meta={"sink": True})
    assert entail.grade("flow", repo, spec, sink) == "full"
    assert entail.grade("flow", repo, spec, src(repo, "orders/repository.py", 10, meta={"sink": True})) == "none"


# -- runs ---------------------------------------------------------------------------------

def run(locator: str, outcome: str = "pass", eid: str = "exp_1", excerpt: str = "3 passed") -> dict:
    return {"source_type": "test_result", "locator": locator, "excerpt": excerpt,
            "meta": {"outcome": outcome, "experiment_id": eid}}


def test_test_run_claims_must_name_the_run_and_be_exercised_by_it(repo):
    r = run("run exp_1: py -m pytest -q tests/test_pricing.py::test_compute_total")
    spec = {"command": ["tests/test_pricing.py::test_compute_total"], "experiment": "exp_1"}
    subjects = ["orders/pricing.py::compute_total()", "tests/test_pricing.py"]
    assert entail.grade("test_run", repo, spec, r, subjects=subjects) == "full"
    # reached through imports (test_service -> orders.service -> orders.pricing)
    r2 = run("run exp_2: py -m pytest -q tests/test_service.py::test_place_and_fetch_roundtrip", eid="exp_2")
    assert entail.grade("test_run", repo, {"command": ["tests/test_service.py::test_place_and_fetch_roundtrip"]}, r2,
                        subjects=["orders/pricing.py::apply_discount()"]) == "full"
    # a run the claim does not name
    assert entail.grade("test_run", repo, {"command": ["tests/test_other.py"]}, r, subjects=subjects) == "none"
    # the claim's run, but a subject it never reaches
    assert entail.grade("test_run", repo, spec, r, subjects=[*subjects, "orders/api.py::get_order_handler()"]) \
        == "partial"


def test_general_claims_and_runs_use_term_coverage(repo):
    # term coverage makes evidence relevant (partial), never a verification (D31)
    pricing = run("run exp_p: python -m pytest -q tests/test_pricing.py")
    assert entail.grade("general", repo, {}, pricing, text="the pricing tests pass") == "partial"
    assert entail.grade("general", repo, {}, pricing, text="Orders are encrypted with AES-256") == "none"
    line = src(repo, "orders/pricing.py", 6, 8)
    g = entail.assess("general", repo, {}, line, text="compute_total applies the discount")
    assert (g.grade, g.code) == ("partial", "coverage") and "does not check order" in g.reason
    # a verbatim quote of the cited lines is the one way a general claim is full
    quote = 'orders/pricing.py:6-8 contains: subtotal = sum(i["price"] * i["qty"] for i in items)'
    assert entail.grade("general", repo, {}, line, text=quote) == "full"
    assert entail.grade("general", repo, {}, line, text="compute_total does not apply the discount") == "partial"
    assert entail.grade("general", repo, {}, line, text="only compute_total applies the discount") == "partial"
    assert entail.grade("general", repo, {}, line, text="Orders are stored in PostgreSQL") == "none"
    assert entail.grade("general", repo, {}, line, text="compute_total applies the discount (orders/api.py:18)") \
        == "partial"  # it cites another location
    assert entail.grade("general", repo, {}, line, text="It works.") == "none"  # nothing checkable
    g = entail.assess("general", repo, {}, line, text="compute_total applies the discount")
    assert "heuristic" in g.reason  # the grade says what it is


def test_documentary_kinds_are_partial_at_most_and_need_attribution(repo):
    adr = src(repo, "docs/adr/0001-sqlite-persistence.md", 1, 7, source_type="design_doc")
    text = ("Decision record docs/adr/0001-sqlite-persistence.md (status: accepted) explains it: We persist orders "
            "in SQLite through `OrderRepository` only. No other module")
    assert entail.grade("decision", repo, {}, adr, text=text) == "partial"
    assert entail.grade("decision", repo, {}, adr, text=text.replace("SQLite", "PostgreSQL")) == "none"
    assert entail.grade("decision", repo, {}, adr, text="Decision record docs/adr/0001-sqlite-persistence.md "
                                                        "explains it: orders are not persisted") == "none"
    commit = {"source_type": "git_history", "locator": "commit " + "a1b2c3d4" * 5, "commit_sha": "a1b2c3d4" * 5,
              "excerpt": "Speed up pricing"}
    assert entail.grade("history", repo, {}, commit, text="`x` lines 1-3 were changed in a1b2c3d4a1 (2026): Speed up "
                                                          "pricing") == "partial"
    assert entail.grade("history", repo, {}, commit, text="`x` was changed in 0000000000 (2026)") == "none"
    assert entail.grade("tests", repo, {}, src(repo, "tests/test_pricing.py", 12, meta={"test": "test_compute_total"}),
                        text="Test code statically reaches `compute_total`") == "partial"


def test_pointers_and_blank_lines_are_never_support(repo):
    ptr = {"source_type": "search_result", "locator": "https://x", "excerpt": "compute_total applies the discount"}
    assert entail.grade("general", repo, {}, ptr, text="compute_total applies the discount") == "none"
    blank = {"source_type": "source_code", "path": "orders/api.py", "line_start": 2, "line_end": 2,
             "locator": "orders/api.py:2", "content_hash": evmod.content_hash(""), "excerpt": "", "meta": {}}
    assert entail.grade("general", repo, {}, blank, text="api") == "none"
    assert entail.grade("relation", repo, {"target_label": "x()", "at": "orders/api.py:2"}, blank,
                        subjects=["orders/api.py::f()", "orders/api.py::x()"]) == "none"


def test_call_site_contract(repo):
    g = entail.call_site(repo, "orders/api.py", 18, "place_order()", caller="create_order_handler()")
    assert (g.grade, g.code) == ("full", "call")
    assert entail.call_site(repo, "orders/api.py", 999, "x").code == "unreadable"
    assert entail.call_site(repo, "orders/api.py", 2, "x").code == "blank"
    assert entail.GRADES == ("none", "partial", "full") and entail.at_least("full", "partial")


@pytest.mark.parametrize("name, caller_dir, text, grade, code", [
    ("imported from the target's package", "com/other", "import com.mod.Spirits;\n", "full", "call"),
    ("same-named class of another package", "com/other", "import com.thirdparty.Spirits;\n", "none", "wrong_module"),
    ("wildcard import of the target's package", "com/other", "import com.mod.*;\n", "full", "call"),
    ("wildcard import of a package that only ends like it", "com/other", "import od.*;\n", "partial", "unbound"),
    ("same package", "com/mod", "", "full", "call"),
    ("not imported", "com/other", "", "partial", "unbound"),
])
def test_java_class_qualified_calls_resolve_the_class_by_package(name, caller_dir, text, grade, code):
    pkg = caller_dir.replace("/", ".")
    src = f"package {pkg};\n{text}class C {{ void f() {{ Spirits.spawn(3); }} }}\n"
    line = src.splitlines().index("class C { void f() { Spirits.spawn(3); } }") + 1
    g = entail._other_call_grade(src, f"src/main/java/{caller_dir}/C.java", line, "spawn",
                                 target_path="src/main/java/com/mod/Spirits.java")
    assert (g.grade, g.code) == (grade, code), name


@pytest.mark.parametrize("qualifier, grade, code", [
    ("com.mod.Spirits", "full", "call"), ("com.other.Spirits", "none", "wrong_module"),
    ("this.Spirits", "partial", "method_unresolved"), ("Outer.Spirits", "partial", "method_unresolved"),
])
def test_java_dotted_qualifiers_are_packages_only_when_written_like_one(qualifier, grade, code):
    src = f"package com.other;\nclass C {{ void f() {{ {qualifier}.spawn(3); }} }}\n"
    g = entail._other_call_grade(src, "src/main/java/com/other/C.java", 2, "spawn",
                                 target_path="src/main/java/com/mod/Spirits.java")
    assert (g.grade, g.code) == (grade, code)


# -- written text binds every role (docs/DESIGN.md D31) ------------------------------------------

@pytest.mark.parametrize("text, target, roles", [
    ("create_order_handler calls save", "save", ("create_order_handler", "save")),
    ("`place_order` calls `OrderRepository.save`", "save", ("place_order", "OrderRepository.save")),
    ("OrderRepository.save calls place_order", "place_order", ("OrderRepository.save", "place_order")),
    ("validate_items is called by place_order", "validate_items", ("place_order", "validate_items")),
    ("place_order validate_items'ı çağırır", None, ("place_order", "validate_items")),
    ("validate_items, place_order tarafından çağrılır", None, ("place_order", "validate_items")),
    ("orders/api.py calls place_order", "place_order", (None, "place_order")),  # a file is not a caller name
    # compound sentences: the clause and the side that hold the target
    ("validate_items and compute_total are called in place_order", "compute_total", ("place_order", "compute_total")),
    ("place_order calls validate_items and compute_total", "compute_total", ("place_order", "compute_total")),
    ("create_order_handler calls place_order, and get_order_handler calls fetch_order", "fetch_order",
     ("get_order_handler", "fetch_order")),
    # Turkish: the accusative marks the callee, whatever the word order
    ("place_order, validate_items ve compute_total'ı çağırır", "compute_total", ("place_order", "compute_total")),
    ("create_order_handler, get_repo ile place_order'ı çağırır", "place_order", ("create_order_handler", "place_order")),
    ("validate_items'ı place_order çağırır", None, ("place_order", "validate_items")),
    ("validate_items fonksiyonunu place_order çağırır", None, ("place_order", "validate_items")),
    # no clear form: no roles (never a guess that could be reversed)
    ("place_order is what create_order_handler calls", None, (None, None)),
    ("place_order'ı çağıran fonksiyon create_order_handler", None, (None, None)),
    ("create_order_handler'ın çağırdığı fonksiyon place_order", "place_order", (None, None)),
    ("Both create_order_handler and get_order_handler call get_repo", "get_repo", (None, None)),
    ("place_order ve validate_items save'i çağırır", "save", (None, None)),
    ("create_order_handler calls place_order, which calls save", None, (None, None)),
    # a call verb outranks "using"/"create", and an infinitive is not the relation
    ("create_order_handler calls place_order using get_repo", "place_order", ("create_order_handler", "place_order")),
    ("create_order_handler calls place_order to create the order", "place_order",
     ("create_order_handler", "place_order")),
    ("get_repo creates `OrderRepository`", "OrderRepository", ("get_repo", "OrderRepository")),
])
def test_relation_roles_read_the_direction_the_text_states(text, target, roles):
    assert entail.relation_roles(text, target) == roles


def test_relation_parse_reports_a_target_on_the_calling_side():
    assert entail.relation_parse("place_order calls save", "place_order") == {
        "caller": "place_order", "caller_file": None, "caller_word": None, "callee": None, "reversed": True}
    assert entail.relation_parse("orders/api.py calls place_order", "place_order")["caller_file"] == "orders/api.py"


@pytest.mark.parametrize("text, target, word", [
    ("checkout calls submit", "submit", "checkout"), ("The function checkout calls submit", "submit", "checkout"),
    ("save is called by checkout", "save", "checkout"), ("save, checkout tarafından çağrılır", "save", "checkout"),
    ("checkout, submit'i çağırır", "submit", "checkout"),
])
def test_a_plain_word_caller_is_kept_apart_from_code_names(text, target, word):
    p = entail.relation_parse(text, target)
    assert p["caller"] is None and p["caller_word"] == word and p["callee"] == target


@pytest.mark.parametrize("text, target", [
    ("checkout and quote call submit", "submit"), ("It calls save", "save"), ("save is called by it", "save"),
])
def test_no_plain_word_caller_from_lists_or_pronouns(text, target):
    assert entail.relation_parse(text, target) is None


@pytest.mark.parametrize("text, neg", [
    ("place_order does not call validate_items", "not"), ("compute_total never calls apply_discount", "never"),
    ("place_order doesn't call save", "doesn't"), ("place_order, validate_items'ı çağırmaz", "çağırmaz"),
    ("İndirim eşiği ORDERS_DISCOUNT_THRESHOLD değişkeninden okunmaz", "okunmaz"),
    ("place_order calls validate_items", None), ("`not_found` is read by analyze", None),
])
def test_negation_is_read_from_the_words_around_the_code(text, neg):
    assert entail.negated_text(text) == neg


def test_code_names_skip_files_prose_and_abbreviations():
    names = [n for _, _, n in entail.code_names("see `x.y()` and config.py, e.g. load_settings or `two words`")]
    assert names == ["x.y", "load_settings"]
    assert entail.is_code_shaped("OrderRepository") and not entail.is_code_shaped("Orders")


@pytest.mark.parametrize("text, where, prop", [
    ("`place_order` calls `save` before `validate_items`", "place_order", "save before validate_items in place_order"),
    ("In place_order, `validate_items` runs after `repo.save`", "place_order",
     "repo.save before validate_items in place_order"),
    ("place_order, `save`'i `validate_items`'dan önce çağırır", "place_order",
     "save before validate_items in place_order"),
    ("place_order, `validate_items`'dan sonra `save`'i çağırır", None, "validate_items before save in place_order"),
    ("place_order saves the order before it validates the items", "place_order", None),  # no two calls named
    # "after that" refers back: the order stays as written
    ("`place_order` calls `validate_items`, and after that `save`", "place_order",
     "validate_items before save in place_order"),
    ("`place_order` checks the items with `validate_items` and only after that calls `save`", "place_order",
     "validate_items before save in place_order"),
    ("`place_order` first calls `validate_items`, then `save`", "place_order", "validate_items before save in place_order"),
    ("After `validate_items`, `place_order` calls `save`", "place_order", "validate_items before save in place_order"),
    ("`checkout` creates the `OrderRepository` before it calls `submit`", "checkout",
     "OrderRepository before submit in checkout"),
    ("`place_order` önce `validate_items`'ı, sonra `save`'i çağırır", "place_order",
     "validate_items before save in place_order"),
    # unsure or negated: no proposition, never a reversed one
    ("`place_order` calls `validate_items` and then, after computing the total, `save`", "place_order", None),
    ("`place_order` never calls `save` before `validate_items`", "place_order", None),
    ("`place_order` does not call `validate_items` before `save`", "place_order", None),
])
def test_order_proposition_from_written_text(text, where, prop):
    assert entail.order_proposition(text, where) == prop


def test_free_text_relation_is_graded_from_the_caller_the_text_names(repo):
    spec = {"free_text": True, "target_label": "place_order", "at": "orders/service.py:22", "symbol": "place_order"}
    line = src(repo, "orders/service.py", 22)
    g = entail.assess("relation", repo, spec, line, text="OrderRepository.save calls place_order",
                      subjects=["orders/service.py"])
    assert (g.grade, g.code) == ("none", "outside_caller") and "not inside `save`" in g.reason
    ok = {**spec, "target_label": "validate_items", "symbol": "validate_items", "at": "orders/service.py:20"}
    assert entail.grade("relation", repo, ok, src(repo, "orders/service.py", 20),
                        text="validate_items is called by place_order", subjects=["orders/service.py"]) == "full"
    # the target the claim names must be the callee of the text, not its caller
    rev = {**ok, "target_label": "place_order", "symbol": "place_order"}
    g = entail.assess("relation", repo, rev, src(repo, "orders/service.py", 20),
                      text="place_order is called by validate_items", subjects=["orders/service.py"])
    assert g.grade != "full"


def test_free_text_config_claim_is_bound_to_what_the_read_sets(repo):
    def cfg(text, var, line):
        return entail.assess("config", repo, {"free_text": True, "env": var, "symbol": var},
                             src(repo, "orders/config.py", line), text=text, subjects=["orders/config.py"])

    assert cfg("The discount threshold is read from ORDERS_DISCOUNT_THRESHOLD", "ORDERS_DISCOUNT_THRESHOLD", 7).grade \
        == "full"
    assert cfg("The maximum number of items per order is read from ORDERS_MAX_ITEMS", "ORDERS_MAX_ITEMS", 6).grade \
        == "full"
    assert cfg("orders/config.py reads ORDERS_DATABASE_URL", "ORDERS_DATABASE_URL", 5).grade == "full"  # no subject
    g = cfg("The discount threshold is read from ORDERS_MAX_ITEMS", "ORDERS_MAX_ITEMS", 6)
    assert g.grade == "partial" and "binds ORDERS_MAX_ITEMS to MAX_ITEMS_PER_ORDER" in g.reason
    res = entail.config_binding(repo, src(repo, "orders/config.py", 6), "ORDERS_MAX_ITEMS",
                                "The discount threshold is read from ORDERS_MAX_ITEMS")
    assert not res["ok"] and res["alt"]["names"][0] == "DISCOUNT_THRESHOLD"
    assert res["why"] == ("orders/config.py:6 binds ORDERS_MAX_ITEMS to MAX_ITEMS_PER_ORDER; DISCOUNT_THRESHOLD "
                          "reads ORDERS_DISCOUNT_THRESHOLD at orders/config.py:7 (scope: environment reads in "
                          "orders/config.py)")
    # generated (not written) text is not held to it: the analysis states the binding by construction
    assert entail.grade("config", repo, {"env": "ORDERS_MAX_ITEMS"}, src(repo, "orders/config.py", 6),
                        text="The discount threshold is read from ORDERS_MAX_ITEMS") == "full"


def test_env_bindings_name_targets_keys_keywords_and_definitions():
    tree = entail._py_tree("import os\n\nclass S:\n    def __init__(self):\n        self.url = os.getenv('DB_URL')\n\n"
                           "def opts():\n    return {'retries': int(os.environ['RETRIES']),\n"
                           "            'x': dict(timeout=os.environ.get('TIMEOUT'))}\n")
    got = {b["var"]: b["names"] for b in entail.env_bindings(tree)}
    assert got == {"DB_URL": ["url", "__init__", "S"], "RETRIES": ["retries", "opts"],
                   "TIMEOUT": ["timeout", "x", "opts"]}


def test_order_claims_are_checked_over_the_whole_function_body(repo):
    body = src(repo, "orders/service.py", 19, 22)
    good = entail.assess("behaviour", repo, {"proposition": "validate_items before save in place_order", "holds": True},
                         body, text="x")
    assert (good.grade, good.code) == ("full", "order") and "line 20" in good.reason and "line 22" in good.reason
    rev = entail.assess("behaviour", repo, {"proposition": "save before validate_items in place_order", "holds": True},
                        body, text="x")
    assert (rev.grade, rev.code) == ("none", "reversed")
    # analysis records the proposition as asked and whether it holds: holds=False states the reverse
    assert entail.grade("behaviour", repo, {"proposition": "save before validate_items in place_order",
                                            "holds": False}, body, text="x") == "full"
    miss = entail.assess("behaviour", repo, {"proposition": "validate_items before get in place_order", "holds": True},
                         body, text="x")
    assert (miss.grade, miss.code) == ("none", "no_call") and "calls through other names" in miss.reason
    # a regex behaviour claim: the cited line matches the pattern
    assert entail.grade("behaviour", repo, {"pattern": r"repo\.save\("}, src(repo, "orders/service.py", 22),
                        text="x") == "full"


def test_caller_scope_reads_the_whole_body_and_says_what_it_did_not_follow(repo):
    hit = entail.caller_scope(repo, "place_order()", "compute_total", path="orders/service.py")
    assert hit["calls"] == [("orders/service.py", 21)] and hit["scope"] == "place_order (orders/service.py:19-22)"
    miss = entail.caller_scope(repo, "create_order_handler", "save", path="orders/api.py")
    assert miss["calls"] == [] and miss["miss"] == ("no direct call to save in create_order_handler "
                                                    "(orders/api.py:16-21); calls through other names are not followed")
    # a method of another file, found through the files the caller is defined in
    other = entail.caller_scope(repo, "OrderRepository.save", "place_order", path="orders/service.py",
                                files=["orders/repository.py"])
    assert other["calls"] == [] and "OrderRepository.save (orders/repository.py:15-20)" in other["scope"]
    assert entail.caller_scope(repo, "no_such_function", "save", path="orders/api.py") is None


def test_typed_kinds_are_the_ones_with_a_mechanical_check():
    assert entail.typed("relation", {"target_label": "save"}) and not entail.typed("relation", {})
    assert entail.typed("config", {"env": "X"}) and entail.typed("location", {}, "`f` is defined at a.py:1-2")
    assert entail.typed("behaviour", {"proposition": "a before b in f", "holds": True})
    assert not entail.typed("general", {}) and not entail.typed("behaviour", {"proposition": "a before b in f"})


def _written(repo, kind, text, cite, **spec):
    path, _, rng = cite.rpartition(":")
    a, _, b = rng.partition("-")
    return entail.assess(kind, repo, {"free_text": True, **spec}, src(repo, path, int(a), int(b or a)), text=text,
                         subjects=[path])


def test_written_text_beyond_the_typed_check_is_not_verified(repo):
    def rel(text, target, at):
        return _written(repo, "relation", text, at, target_label=target, symbol=target, at=at)

    assert rel("place_order calls validate_items", "validate_items", "orders/service.py:20").grade == "full"
    # a negation: the check proves the positive statement, so the negated text is never verified
    for text in ("place_order does not call validate_items", "place_order, validate_items'ı çağırmaz"):
        g = rel(text, "validate_items", "orders/service.py:20")
        assert g.grade == "partial" and "the text is negated" in g.reason, text
    g = _written(repo, "location", "`place_order` is not defined in orders/service.py", "orders/service.py:19-22",
                 symbol="place_order")
    assert g.grade == "partial" and "negated ('not')" in g.reason
    # an order, a count or 'only' that a relation check does not look at
    for text, why in (("place_order calls compute_total before validate_items", "an order ('before')"),
                      ("place_order calls compute_total twice", "a count ('twice')"),
                      ("Only place_order calls compute_total", "states 'only'")):
        g = rel(text, "compute_total", "orders/service.py:21")
        assert g.grade == "partial" and why in g.reason, text
    # roles the text does not state in one clear form cannot be bound
    g = rel("Both create_order_handler and get_order_handler call get_repo", "get_repo", "orders/api.py:18")
    assert g.grade == "partial" and "does not state one caller" in g.reason
    assert rel("validate_items and compute_total are called in place_order", "compute_total",
               "orders/service.py:21").grade == "full"
    # a plain-word caller must be the definition around the cited line (or its file)
    assert rel("handle calls validate_items", "validate_items", "orders/flow.py:11").grade == "full"
    g = rel("The handler calls validate_items", "validate_items", "orders/flow.py:11")
    assert g.grade == "partial" and "caller `handler` is not a definition around the cited lines" in g.reason
    assert rel("orders/flow.py calls validate_items", "validate_items", "orders/flow.py:11").grade == "full"
    assert rel("orders/api.py calls validate_items", "validate_items", "orders/flow.py:11").grade == "partial"


def test_written_config_default_is_compared_with_the_read(repo):
    def cfg(text):
        return _written(repo, "config", text, "orders/config.py:7", env="ORDERS_DISCOUNT_THRESHOLD",
                        symbol="ORDERS_DISCOUNT_THRESHOLD")

    assert cfg("The discount threshold is read from ORDERS_DISCOUNT_THRESHOLD and defaults to 100.0").grade == "full"
    assert cfg("The discount threshold, 100 by default, is read from ORDERS_DISCOUNT_THRESHOLD").grade == "full"
    g = cfg("The discount threshold is read from ORDERS_DISCOUNT_THRESHOLD and defaults to 50")
    assert g.grade == "partial" and "the text states 50; the read at orders/config.py:7 is `os.environ.get(" in g.reason
    g = cfg("The discount threshold is not read from ORDERS_DISCOUNT_THRESHOLD")
    assert g.grade == "partial" and "negated" in g.reason


def test_a_quote_verifies_only_the_quoted_text(repo):
    def gen(text, at):
        return _written(repo, "general", text, at)

    assert gen("orders/pricing.py:15 contains: return subtotal", "orders/pricing.py:15").grade == "full"
    # the name of the definition around the cited lines is a locator too, and framing words add nothing
    assert gen("apply_discount at orders/pricing.py:15 contains: return subtotal", "orders/pricing.py:15").grade \
        == "full"
    assert gen("The cited line orders/pricing.py:15 contains: return subtotal", "orders/pricing.py:15").grade \
        == "full"
    g = gen("apply_discount returns the subtotal unchanged above the threshold; orders/pricing.py:15 contains: "
            "return subtotal", "orders/pricing.py:15")
    assert (g.grade, g.code) == ("partial", "quote_rest") and "the quote verifies only the quoted text" in g.reason
    g = gen("apply_discount never applies a discount; orders/pricing.py:14 contains: return round(subtotal * 0.9, 2)",
            "orders/pricing.py:14")
    assert g.grade == "partial"
    # generated text (the analysis' own claims) is not written text: its quote stands
    assert entail.grade("general", repo, {}, src(repo, "orders/pricing.py", 15),
                        text="`apply_discount` returns it; orders/pricing.py:15 contains: return subtotal") == "full"


def test_calls_in_nested_functions_and_lambdas_have_no_static_order(repo):
    def order(prop, where):
        a, b = {"handle": (8, 12), "handle_lambda": (15, 18)}[where]
        return entail.assess("behaviour", repo, {"proposition": f"{prop} in {where}", "holds": True},
                             src(repo, "orders/flow.py", a, b), text="x")

    for where in ("handle", "handle_lambda"):
        for prop in ("validate_items before save_items", "save_items before validate_items"):
            g = order(prop, where)
            assert (g.grade, g.code) == ("partial", "nested") and "nested in" in g.reason, (where, prop)
    tree = entail._py_tree((repo / "orders" / "flow.py").read_text(encoding="utf-8"))
    fn = entail._defs_named(tree, "handle")[0]
    assert entail.calls_in(tree, fn, "save_items") == [10]
    assert entail.calls_in(tree, fn, "save_items", nested=False) == []
    assert entail.calls_in(tree, fn, "save_items", nested=True) == [10]


def test_module_constants_are_defined_by_their_assignment(repo):
    def loc(sym, at):
        return _written(repo, "location", f"`{sym.rpartition('::')[2]}` is defined in orders/config.py", at,
                        symbol=sym)

    assert loc("DISCOUNT_THRESHOLD", "orders/config.py:7").grade == "full"
    assert loc("MAX_ITEMS_PER_ORDER", "orders/config.py:7").grade == "partial"  # assigned at 6, not 7
    assert loc("orders/config.py::DISCOUNT_THRESHOLD", "orders/config.py:7").grade == "full"
    g = entail.assess("location", repo, {"symbol": "orders/api.py::place_order"},
                      src(repo, "orders/service.py", 19, 22), text="`place_order`")
    assert g.grade == "none" and "orders/api.py" in g.reason
    tree = entail._py_tree("import os as o\nX: int = 1\nclass C:\n    Y = 2\n    def m(self):\n        self.z = 3\n")
    assert entail.assignment_spans(tree, "X") == [(2, 2)] and entail.assignment_spans(tree, "C.Y") == [(4, 4)]
    assert entail.assignment_spans(tree, "z") == []  # a method's attribute is not a definition of the module
    assert all(entail.binds_name(tree, n) for n in ("o", "X", "Y", "m", "z", "self"))
    assert not entail.binds_name(tree, "os_path")


def test_calls_on_the_receiver_the_claim_writes(repo):
    def rel(text, target, at):
        return _written(repo, "relation", text, at, target_label=target, symbol=target, at=at)

    g = rel("Cart.add calls self._check", "self._check", "orders/flow.py:23")
    assert (g.grade, g.code) == ("full", "receiver_call")
    g = rel("Cart.add calls _check", "_check", "orders/flow.py:23")
    assert (g.grade, g.code) == ("full", "self_call") and "which defines `_check`" in g.reason
    assert rel("place_order calls repo.save", "repo.save", "orders/service.py:22").code == "receiver_call"
    assert rel("place_order calls other.save", "other.save", "orders/service.py:22").grade == "partial"
    # an owner that is not the line's class: the text names a method that is not there
    g = rel("OrderRepository.place_order calls validate_items", "validate_items", "orders/service.py:20")
    assert (g.grade, g.code) == ("none", "outside_caller") and "not inside a class `OrderRepository`" in g.reason
