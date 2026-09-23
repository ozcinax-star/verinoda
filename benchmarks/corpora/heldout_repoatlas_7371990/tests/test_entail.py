"""Entailment grades: does the evidence state the claim (full), concern it (partial), or neither (none)?"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas import entail  # noqa: E402
from repoatlas import evidence as evmod  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "orders_app"


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    r = tmp_path_factory.mktemp("entail") / "orders_app"
    shutil.copytree(EXAMPLE, r, ignore=shutil.ignore_patterns(".repoatlas", "__pycache__", "*.pyc", "*.db"))
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
    pricing = run("run exp_p: python -m pytest -q tests/test_pricing.py")
    assert entail.grade("general", repo, {}, pricing, text="the pricing tests pass") == "full"
    assert entail.grade("general", repo, {}, pricing, text="Orders are encrypted with AES-256") == "none"
    line = src(repo, "orders/pricing.py", 6, 8)
    assert entail.grade("general", repo, {}, line, text="compute_total applies the discount") == "full"
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
