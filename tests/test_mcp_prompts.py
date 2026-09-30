"""Tests for the MCP prompts (verinoda.mcp.prompts): the ready workflows review, onboarding, debug and
pre_merge, as the server lists and fills them in and as ``verinoda mcp prompts`` prints them.

A prompt names Verinoda tool calls; every call it names must be one the served menu can make, with
arguments the tool accepts. The stdio round trip (prompts/list over the wire) is in tests/test_mcp.py.
"""

import json
from pathlib import Path

import pytest

from verinoda import cli
from verinoda.mcp import prompts as P
from verinoda.mcp import server as mcp_server

anyio = pytest.importorskip("anyio")

ARGS = {"review": {"base": "v1.0"}, "onboarding": {"topic": "checkout"},
        "debug": {"symptom": "totals are wrong", "repro": "python -m pytest -q tests/test_pricing.py"},
        "pre_merge": {"base": "develop"}}


@pytest.fixture
def plain(tmp_path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    return d


def _menu(repo: Path, profile: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return mcp_server.listed_tools(repo, profile), mcp_server.served_tools(repo, profile)


def _schemas(repo: Path) -> dict[str, dict]:
    srv = mcp_server.build_server(repo, profile="full")
    return {t.name: (getattr(t, "input_schema", None) or getattr(t, "inputSchema")) for t in anyio.run(srv.list_tools)}


def test_every_step_names_a_real_tool_with_arguments_it_accepts(plain):
    schemas = _schemas(plain)
    for name in P.PROMPT_NAMES:
        for args in ({}, ARGS[name]):
            if P.missing_required(name, args):
                continue
            _, steps, _ = P._steps(name, args)
            assert steps and steps[0].tool == "index_update", name
            for step in steps:
                assert step.tool in mcp_server.TOOL_NAMES, (name, step.tool)
                props = set(schemas[step.tool].get("properties", {}))
                assert set(step.arguments) <= props, (name, step.tool, step.arguments)


def test_full_profile_names_every_tool_directly(plain):
    listed, served = _menu(plain, "full")
    for name in P.PROMPT_NAMES:
        text = P.render(name, ARGS[name], listed=listed, served=served)
        assert "run_tool" not in text and "in a terminal" not in text, name
        _, steps, _ = P._steps(name, ARGS[name])
        for step in steps:
            assert f"{step.tool} {json.dumps(step.arguments, ensure_ascii=False)}" in text, (name, step.tool)


def test_core_profile_routes_through_run_tool_and_the_cli(plain):
    listed, served = _menu(plain, "core")
    review = P.render("review", {}, listed=listed, served=served)
    assert 'run_tool {"name": "change_review", "arguments": {}}' in review
    assert 'code_check {"diff": "HEAD"}' in review
    # no decision records in this project: the core profile does not serve decision_check, so no step names it
    assert "decision_check" not in review
    debug = P.render("debug", ARGS["debug"], listed=listed, served=served)
    assert '`verinoda debug start "totals are wrong" -- python -m pytest -q tests/test_pricing.py` in a terminal' \
        in debug
    assert "verinoda debug try --hypothesis" in debug and "debug_start {" not in debug
    lines = [ln for ln in debug.splitlines() if ln[:1].isdigit()]
    assert [ln.split(".")[0] for ln in lines] == [str(i) for i in range(1, len(lines) + 1)]  # numbered 1..n


def test_debug_splits_the_repro_into_an_argument_list(plain):
    listed, served = _menu(plain, "full")
    text = P.render("debug", ARGS["debug"], listed=listed, served=served)
    assert '"command": ["python", "-m", "pytest", "-q", "tests/test_pricing.py"]' in text
    assert P.missing_required("debug", {}) == ["symptom"] and P.missing_required("debug", {"symptom": " "})
    assert P.missing_required("debug", {"symptom": "x"}) == []


def test_debug_repro_keeps_windows_backslashes(plain):
    listed, served = _menu(plain, "full")
    repro = r'"C:\Program Files\py.exe" -m pytest tests\test_x.py'
    assert P.split_repro(repro) == [r"C:\Program Files\py.exe", "-m", "pytest", r"tests\test_x.py"]
    text = P.render("debug", {"symptom": "boom", "repro": repro}, listed=listed, served=served)
    assert json.dumps([r"C:\Program Files\py.exe", "-m", "pytest", r"tests\test_x.py"]) in text


def test_debug_cli_fallback_quotes_each_repro_argument(plain):
    listed, served = _menu(plain, "core")
    text = P.render("debug", {"symptom": "boom", "repro": r'"C:\x y\python.exe" -m pytest'},
                    listed=listed, served=served)
    assert r'`verinoda debug start "boom" -- "C:\x y\python.exe" -m pytest`' in text


@pytest.mark.parametrize("profile", ["core", "full"])
@pytest.mark.parametrize("args, why", [({"symptom": "   "}, "needs symptom"),
                                       ({"symptom": "x", "repro": "python -c 'print(1)"}, "No closing quotation")])
def test_server_refuses_what_the_cli_refuses(plain, profile, args, why):
    srv = mcp_server.build_server(plain, profile=profile)
    with pytest.raises(Exception) as exc:
        anyio.run(srv.get_prompt, "debug", args)
    assert why in str(exc.value) and "Error rendering" not in str(exc.value)
    assert P.argument_problems("debug", args)


def test_server_lists_and_fills_in_the_prompts_without_changing_the_tool_menu(plain):
    srv = mcp_server.build_server(plain)
    listed = anyio.run(srv.list_prompts)
    assert [p.name for p in listed] == list(P.PROMPT_NAMES)
    for p in listed:
        assert p.description == P.DESCRIPTIONS[p.name]
        assert [(a.name, bool(a.required), a.description) for a in (p.arguments or [])] == \
            [(a, r, d) for a, d, r in P.ARGUMENTS[p.name]]
    got = anyio.run(srv.get_prompt, "review", {"base": "v1.0"})
    listed_names, served = _menu(plain, "core")
    assert got.messages[0].content.text == P.render("review", {"base": "v1.0"}, listed=listed_names, served=served)
    assert sorted(t.name for t in anyio.run(srv.list_tools)) == sorted([*mcp_server.CORE_DIRECT, mcp_server.GATEWAY])


def test_cli_lists_and_prints_prompts(plain, capsys):
    assert cli.main(["mcp", "prompts", "--repo", str(plain), "--json"]) == 0
    cat = json.loads(capsys.readouterr().out)
    assert cat["profile"] == "core" and [c["name"] for c in cat["prompts"]] == list(P.PROMPT_NAMES)
    assert cli.main(["mcp", "prompts", "pre_merge", "--arg", "base=develop", "--profile", "full",
                     "--repo", str(plain), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    listed, served = _menu(plain, "full")
    assert out["arguments"] == {"base": "develop"} and out["profile"] == "full"
    assert out["text"] == P.render("pre_merge", {"base": "develop"}, listed=listed, served=served)
    assert cli.main(["mcp", "prompts", "review", "--repo", str(plain)]) == 0
    assert capsys.readouterr().out.startswith("Review the uncommitted change")


@pytest.mark.parametrize("argv", [["debug"], ["nope"], ["review", "--arg", "topic=x"], ["review", "--arg", "base"],
                                  ["debug", "--arg", "symptom=  "],
                                  ["debug", "--arg", "symptom=x", "--arg", "repro=echo 'unterminated"]])
def test_cli_refuses_bad_prompt_arguments(plain, capsys, argv):
    assert cli.main(["mcp", "prompts", *argv, "--repo", str(plain)]) == 2
    assert "error:" in capsys.readouterr().err
