"""The extractors ported from Graphify v0.9.73 (COBOL, Erlang, R, Solidity, VB.NET): what each reads from a small
fixture of its own, that a missing optional grammar degrades to a reported, not extracted file (never a crash),
and the Verinoda layers a new language needs (spans, the lang: filter, exact names, the scan report)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import buildlock, grammars  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "languages"
# language -> the module its grammar is imported from (None: a regex extractor, no grammar)
GRAMMAR_MODULE = {"cobol": None, "erlang": "tree_sitter_language_pack", "r": "tree_sitter_language_pack",
                  "solidity": "tree_sitter_solidity", "vbnet": "tree_sitter_vb_dotnet"}


def _needs(lang: str) -> None:
    mod = GRAMMAR_MODULE[lang]
    if mod is not None:
        pytest.importorskip(mod)


def _extract(tmp_path: Path, lang: str) -> dict:
    from verinoda.project_index.extract import extract

    root = tmp_path / lang
    shutil.copytree(FIX / lang, root)
    files = sorted(p for p in root.iterdir() if p.is_file())
    return extract(files, cache_root=tmp_path / "cache", parallel=False)


def _labelled(res: dict) -> tuple[set[str], set[tuple[str, str, str]]]:
    label = {n["id"]: n["label"] for n in res["nodes"]}
    edges = {(label.get(e["source"], e["source"]), e["relation"], label.get(e["target"], e["target"]))
             for e in res["edges"]}
    return set(label.values()), edges


@pytest.fixture(autouse=True)
def _fresh_grammar_checks(monkeypatch):
    """Each test sees the grammars as its own sys.modules has them."""
    monkeypatch.setattr(grammars, "_PROBLEMS", {})


# -- what each extractor reads -----------------------------------------------------------------------------------

def test_cobol_programs_paragraphs_copybooks_and_calls(tmp_path):
    labels, edges = _labelled(_extract(tmp_path, "cobol"))
    assert {"BILLING", "TAXCALC", "MAIN-PARA", "READ-CUSTOMER", "COMPUTE-TOTAL", "COMPUTE-EXIT",
            "WS-TOTAL", "CUSTOMER-RECORD"} <= labels
    assert ("MAIN-PARA", "calls", "READ-CUSTOMER") in edges
    # PERFORM A THRU B performs both ends
    assert {("MAIN-PARA", "calls", "COMPUTE-TOTAL"), ("MAIN-PARA", "calls", "COMPUTE-EXIT")} <= edges
    assert ("BILLING", "imports_from", "CUSTREC.cpy") in edges        # COPY CUSTREC.
    assert ("MAIN-PARA", "calls", "TAXCALC") in edges                 # CALL 'TAXCALC', another program's file


def test_erlang_functions_exports_includes_and_remote_calls(tmp_path):
    _needs("erlang")
    labels, edges = _labelled(_extract(tmp_path, "erlang"))
    assert {"shop", "cart", "checkout/1", "apply_discount/1", "total/1", "price/1", "item"} <= labels
    assert ("checkout/1", "calls", "apply_discount/1") in edges       # a local call
    assert ("checkout/1", "calls", "total/1") in edges                # cart:total(Items), module and arity
    assert ("shop", "imports_from", "shop.hrl") in edges              # -include("shop.hrl").
    assert ("shop", "exports", "checkout/1") in edges
    assert ("shop", "implements", "gen_server") in edges


def test_r_functions_sourced_files_and_calls(tmp_path):
    _needs("r")
    labels, edges = _labelled(_extract(tmp_path, "r"))
    assert {"summarise_scores()", "report()", "drop_missing()"} <= labels
    assert ("main.R", "imports_from", "utils.R") in edges             # source("utils.R")
    assert ("report()", "calls", "summarise_scores()") in edges
    assert ("summarise_scores()", "calls", "drop_missing()") in edges  # bound through the sourced file only
    assert not any(t in ("mean()", "print()") for _s, r, t in edges if r == "calls")


def test_solidity_contracts_inheritance_imports_and_calls(tmp_path):
    _needs("solidity")
    labels, edges = _labelled(_extract(tmp_path, "solidity"))
    assert {"Token", "Ownable", "transfer()", "mint()", "_move()", "onlyOwner()", "Transfer", "owner"} <= labels
    assert ("Token.sol", "imports_from", "Ownable.sol") in edges
    assert ("Token", "inherits", "Ownable") in edges
    assert ("transfer()", "calls", "_move()") in edges
    assert ("Token", "method", "transfer()") in edges
    assert ("transferOwnership()", "uses", "onlyOwner()") in edges


def test_vbnet_classes_imports_and_calls_across_partial_files(tmp_path):
    _needs("vbnet")
    labels, edges = _labelled(_extract(tmp_path, "vbnet"))
    assert {"OrderService", "PlaceOrder()", "CountItems()", "Validate()", "Shop"} <= labels
    assert ("OrderService.vb", "imports", "System.Collections.Generic") in edges
    assert ("PlaceOrder()", "calls", "CountItems()") in edges
    assert ("PlaceOrder()", "calls", "Validate()") in edges           # declared in the other partial file


def test_razor_functions_block_methods(tmp_path):
    from verinoda.project_index.extract import extract_razor

    p = tmp_path / "Index.cshtml"
    p.write_text("@page\n<h1>Hi</h1>\n@functions {\n    public string Greet(string name) {\n"
                 "        return Format(name);\n    }\n}\n", encoding="utf-8")
    labels = {n["label"] for n in extract_razor(p)["nodes"]}
    assert any(lab.startswith("Greet") for lab in labels)


def test_ocaml_classes_methods_and_instance_variables(tmp_path):
    pytest.importorskip("tree_sitter_ocaml")
    from verinoda.project_index.extract import extract_ocaml

    p = tmp_path / "shapes.ml"
    p.write_text("class point x0 = object\n  val mutable x = x0\n  method get_x = x\n"
                 "  method move d = x <- x + d\nend\n", encoding="utf-8")
    labels, edges = _labelled(extract_ocaml(p))
    assert {"point", "get_x", "move", "x"} <= labels
    assert ("point", "method", "get_x") in edges and ("point", "contains", "x") in edges


def test_terraform_redacts_secrets_in_lists_and_sensitive_variables(tmp_path):
    pytest.importorskip("tree_sitter_hcl")
    from verinoda.project_index.extract import extract_terraform

    p = tmp_path / "main.tf"
    p.write_text('variable "db_password" {\n  default = "hunter2"\n}\n'
                 'resource "aws_ecs_task_definition" "t" {\n'
                 '  configs = [{ password = "s3cret" }]\n'
                 '  environment = [{ name = "API_TOKEN", value = "tok123" }]\n}\n', encoding="utf-8")
    text = json.dumps(extract_terraform(p))
    assert "hunter2" not in text and "s3cret" not in text and "tok123" not in text


# -- a missing grammar ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("lang, ext, grammar", [("erlang", ".erl", "erlang"), ("r", ".R", "r"),
                                               ("solidity", ".sol", "solidity"), ("vbnet", ".vb", "vbnet")])
def test_a_missing_grammar_is_an_error_result_never_an_exception(tmp_path, monkeypatch, lang, ext, grammar):
    from verinoda.project_index import extract as ex

    monkeypatch.setitem(sys.modules, GRAMMAR_MODULE[lang], None)   # import fails as when it is not installed
    path = next(p for p in sorted((FIX / lang).iterdir()) if p.suffix == ext)
    res = ex._get_extractor(path)(path)
    assert res["nodes"] == [] and "not installed" in res["error"]
    assert grammars.problem(grammar) == f"{grammars.GRAMMARS[grammar][1]} is not installed"
    whole = _extract(tmp_path, lang)                                   # the whole build goes on
    assert not any(str(n.get("source_file", "")).endswith(ext) for n in whole["nodes"])


def test_grammar_suffixes_match_the_extractors_table():
    from verinoda.project_index.extract import _EXTRA_FOR_EXTENSION

    assert set(grammars.SUFFIXES) == set(_EXTRA_FOR_EXTENSION)
    assert {ext: g for ext, (_lang, g) in grammars.SUFFIXES.items()} == _EXTRA_FOR_EXTENSION


def test_not_extracted_groups_files_by_grammar_with_reason_and_install(monkeypatch):
    monkeypatch.setitem(grammars._PROBLEMS, "solidity", "tree-sitter-solidity is not installed")
    monkeypatch.setitem(grammars._PROBLEMS, "vbnet", None)
    files = ["c/Token.sol", "c/Ownable.sol", "c/Gone.sol", "vb/A.vb", "README.md"]
    out = grammars.not_extracted(files, in_graph={"c/Gone.sol"})
    assert out == [{"language": "Solidity", "grammar": "solidity", "count": 2,
                    "files": ["c/Ownable.sol", "c/Token.sol"], "reason": "tree-sitter-solidity is not installed",
                    "install": 'pip install "verinoda[solidity]"'}]
    assert grammars.describe(out[0]).startswith("2 Solidity file(s) not extracted: tree-sitter-solidity is not "
                                                'installed (pip install "verinoda[solidity]")')


def test_the_extraction_stamp_changes_when_a_grammar_comes_or_goes(monkeypatch):
    monkeypatch.setattr(buildlock, "_STAMP", None)
    monkeypatch.setitem(grammars._PROBLEMS, "solidity", None)
    with_it = buildlock.extraction_stamp()
    monkeypatch.setattr(buildlock, "_STAMP", None)
    monkeypatch.setitem(grammars._PROBLEMS, "solidity", "tree-sitter-solidity is not installed")
    assert buildlock.extraction_stamp() != with_it
    monkeypatch.setattr(buildlock, "_STAMP", None)   # the next test computes it again


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_scan_reports_files_a_missing_grammar_kept_out_and_update_reads_them_once_it_is_there(tmp_path, monkeypatch):
    _needs("solidity")
    from verinoda import index, workflow
    from verinoda.store import open_store

    repo = tmp_path / "proj"
    shutil.copytree(FIX, repo)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                       cwd=repo, check=True, capture_output=True)
    workflow.init(repo)
    st = open_store(repo)
    try:
        monkeypatch.setitem(sys.modules, "tree_sitter_solidity", None)
        monkeypatch.setattr(buildlock, "_STAMP", None)
        res = workflow.scan(st, repo)
        assert not res.get("error")
        sol = [g for g in res["not_extracted"] if g["grammar"] == "solidity"]
        assert sol == [{"language": "Solidity", "grammar": "solidity", "count": 2,
                        "files": ["solidity/Ownable.sol", "solidity/Token.sol"],
                        "reason": "tree-sitter-solidity is not installed",
                        "install": 'pip install "verinoda[solidity]"'}]
        in_graph = index.graph_source_files(repo)
        assert "solidity/Token.sol" not in in_graph and "cobol/billing.cbl" in in_graph
        assert workflow.update(st, repo)["mode"] == "noop"
        # the grammar is installed: the stamp differs, so the next update rebuilds without a changed file
        monkeypatch.delitem(sys.modules, "tree_sitter_solidity")
        monkeypatch.setattr(grammars, "_PROBLEMS", {})
        monkeypatch.setattr(buildlock, "_STAMP", None)
        res = workflow.update(st, repo)
        assert res["index_mode"] == "full" and res["changed_count"] == 0
        assert not [g for g in res.get("not_extracted") or [] if g["grammar"] == "solidity"]
        assert {"solidity/Token.sol", "solidity/Ownable.sol"} <= index.graph_source_files(repo)
    finally:
        st.close()
        monkeypatch.setattr(buildlock, "_STAMP", None)


# -- the Verinoda layers ----------------------------------------------------------------------------------------

def test_spans_from_the_optional_grammars(tmp_path):
    from verinoda import index

    _needs("vbnet")
    _needs("solidity")
    _needs("r")
    vb = index.ts_def_ends((FIX / "vbnet" / "OrderService.vb").read_bytes(), ".vb")
    assert vb[5] == 9 and vb[11] == 13          # End Function, not the blank line after it
    sol = index.ts_def_ends((FIX / "solidity" / "Token.sol").read_bytes(), ".sol")
    assert sol[6] == 25 and sol[11] == 14 and sol[20] == 24
    r = index.ts_def_ends((FIX / "r" / "main.R").read_bytes(), ".r")
    assert r[3] == 6 and r[8] == 10


def test_lang_filter_names_the_new_languages():
    from verinoda.query_filters import lang_matches

    assert lang_matches("solidity", "c/Token.sol") and lang_matches("sol", "c/Token.sol")
    assert lang_matches("vbnet", "a/B.vb") and lang_matches("vb", "a/B.vb")
    assert lang_matches("cobol", "x/CUSTREC.cpy") and lang_matches("cobol", "x/billing.cbl")
    assert lang_matches("erlang", "src/shop.hrl") and lang_matches("r", "R/main.R")
    assert not lang_matches("solidity", "a/B.vb")


def test_an_erlang_function_is_named_exactly_without_its_arity(tmp_path):
    import networkx as nx

    from verinoda import naming, retrieval
    from verinoda.index import Graph

    G = nx.DiGraph()
    md = {"language": "erlang", "kind": "function", "module": "cart", "name": "total", "arity": 1}
    G.add_node("cart_total_1", label="total/1", source_file="cart.erl", source_location="L4", metadata=md,
               file_type="code")
    G.add_node("cart_erl", label="cart.erl", source_file="cart.erl", source_location="L1", file_type="code")
    G.add_edge("cart_erl", "cart_total_1", relation="contains")
    g = Graph(G=G, path=tmp_path / "graph.json", root=tmp_path)
    assert retrieval._names_exactly(g, "total", "cart_total_1")
    assert naming.exact_nodes(g, "total")[0] == ["cart_total_1"]


def test_fixtures_are_plain_lf_text():
    for p in FIX.rglob("*"):
        if p.is_file():
            assert b"\r\n" not in p.read_bytes(), p
            json.dumps(p.read_text(encoding="utf-8"))
