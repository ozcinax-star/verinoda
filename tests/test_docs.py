"""The user-facing docs stay in step with the code they describe.

Checks README.md and docs/{ARCHITECTURE,UPSTREAM,UPGRADING,DESIGN,GENEL-BAKIS}.md
against the CLI parser, the package layout, the MCP tool list, the store schema,
the blocked upstream commands and pyproject's extras. It does not check measured
numbers: those are owned by docs/BENCHMARKS.md and its result files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "verinoda"
DOCS = ROOT / "docs"
OWNED = [ROOT / "README.md"] + [DOCS / f"{n}.md" for n in
                                ("ARCHITECTURE", "UPSTREAM", "UPGRADING", "DESIGN", "GENEL-BAKIS")]


def _text(p: Path) -> str:
    return p.read_bytes().decode("utf-8")


def _section(text: str, heading: str) -> str:
    """The body of the level-2 section ``heading`` (up to the next ``## ``)."""
    start = text.index(f"\n## {heading}")
    end = text.find("\n## ", start + 4)
    return text[start:end if end != -1 else len(text)]


@pytest.mark.parametrize("path", OWNED, ids=lambda p: p.name)
def test_docs_use_lf_line_endings_and_utf8(path):
    data = path.read_bytes()
    assert b"\r" not in data, f"{path.name} has CR bytes (files must be LF)"
    data.decode("utf-8")  # raises on invalid UTF-8


def _cli_commands() -> list[str]:
    from verinoda import cli

    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    return sorted(sub.choices)


def test_readme_commands_table_lists_every_cli_command():
    table = _section(_text(ROOT / "README.md"), "Commands")
    rows = [ln for ln in table.splitlines() if ln.startswith("| `")]
    first_cells = " ".join(ln.split("|")[1] for ln in rows)
    missing = [c for c in _cli_commands() if not re.search(rf"`{re.escape(c)}\b", first_cells)
               and not re.search(rf"/{re.escape(c)}\b", first_cells)]
    assert not missing, f"README Commands table lacks: {missing}"


def test_readme_documents_new_subcommands_and_exit_codes():
    readme = _text(ROOT / "README.md")
    for needle in ("plan draft", "plan check", "resolve \"", "observe", "resolve-call",
                   "benchmark staleness", "critique-eval", "scan <repo> [--force] [--precise] [--scip FILE]"):
        assert needle in readme, needle
    assert "3 \"needs more\"" in readme


def _modules() -> list[str]:
    mods = [p.name for p in PKG.glob("*.py") if p.stem not in ("__init__", "__main__")]
    pkgs = [p.name + "/" for p in PKG.iterdir() if p.is_dir() and (p / "__init__.py").is_file()]
    return sorted(mods + pkgs)


def test_architecture_names_every_module_and_package():
    arch = _text(DOCS / "ARCHITECTURE.md")
    missing = [m for m in _modules() if f"`{m}`" not in arch]
    assert not missing, f"ARCHITECTURE.md module tables lack: {missing}"


@pytest.mark.parametrize("sub", ["references", "runtime"])
def test_architecture_names_every_file_of_the_round3_packages(sub):
    arch = _text(DOCS / "ARCHITECTURE.md")
    files = sorted(p.name for p in (PKG / sub).glob("*.py") if p.stem not in ("__init__", "__main__"))
    missing = [f for f in files if f"`{f}`" not in arch]
    assert not missing, f"ARCHITECTURE.md lacks {sub}/: {missing}"


def test_architecture_names_the_benchmark_harnesses():
    arch = _text(DOCS / "ARCHITECTURE.md")
    for f in ("runner.py", "staleness.py", "critique_eval.py"):
        assert (PKG / "benchmark" / f).is_file() and f"`{f}`" in arch, f


def test_architecture_state_on_disk_lists_the_derived_and_new_files():
    from verinoda import paths

    state = _section(_text(DOCS / "ARCHITECTURE.md"), "State on disk")
    repo = Path("/r")
    names = {paths.search_db_path(repo).name, paths.receiver_calls_path(repo).name,
             paths.lexicon_path(repo).name, paths.scip_index_path(repo).name}
    for needle in (*names, "scip_fresh.json", "plans/", "artifacts/calltrace.jsonl", "http-cache", "atlas.db",
                   "config.json", "install-manifest.json"):
        assert needle in state, needle


def test_schema_version_and_mcp_tool_count_match_the_code():
    from verinoda import store
    from verinoda.mcp.server import TOOL_NAMES

    arch, upg, readme = (_text(p) for p in (DOCS / "ARCHITECTURE.md", DOCS / "UPGRADING.md", ROOT / "README.md"))
    assert f"schema v{store.SCHEMA_VERSION}" in arch
    assert f"v{store.SCHEMA_VERSION}" in upg
    n = len(TOOL_NAMES)
    for name, text in (("ARCHITECTURE.md", arch), ("README.md", readme), ("UPGRADING.md", upg)):
        counts = {int(m) for m in re.findall(r"\b(\d+) tools\b", text)}
        assert counts == {n}, f"{name} states {counts} MCP tools, the server has {n}"


def test_upstream_lists_every_blocked_pass_through_command():
    from verinoda import cli

    up = _text(DOCS / "UPSTREAM.md")
    blocked = set(cli.UPSTREAM_BLOCKED) | set(cli.UPSTREAM_HOME_WRITERS) | set(cli.UPSTREAM_HOME_FLAGS)
    missing = sorted(b for b in blocked if f"`{b}`" not in up)
    assert not missing, f"UPSTREAM.md does not list blocked commands: {missing}"
    assert "install_path_identity_memo" in up and "scip_ingest.py" in up


def test_design_has_one_status_row_per_decision():
    status = _section(_text(DOCS / "DESIGN.md"), "Implementation status (2026-09-23)")
    for n in range(1, 31):
        row = re.search(rf"^\| D{n} \|[^|]*\| (implemented|partial|not done) \|", status, re.M)
        assert row, f"DESIGN.md status table has no row for D{n}"


def test_precise_extra_install_lines_match_pyproject():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib
    extras = tomllib.loads(_text(ROOT / "pyproject.toml"))["project"]["optional-dependencies"]
    (jedi,) = extras["precise"]
    for p in (ROOT / "README.md", DOCS / "UPGRADING.md", DOCS / "GENEL-BAKIS.md"):
        text = _text(p)
        assert f'--with "{jedi}"' in text and "[precise]" in text, p.name
    assert "uv tool install --link-mode copy" in _text(ROOT / "README.md")


def test_genel_bakis_separates_theory_from_measurement():
    tr = _text(DOCS / "GENEL-BAKIS.md")
    theory = _section(tr, "6. Teorik tasarruf ve maliyet modeli (ölçüm değil)")
    measured = _section(tr, "7. Ölçülen sonuçlar")
    assert "Bu bölüm teoriktir" in theory
    assert "docs/BENCHMARKS.md" in measured and "örneklem içi" in measured
    # the scenario arithmetic is shown, and it is right
    for a, b, q in (("24.000", "6.000", "4"), ("24.000", "1.500", "16"), ("75.000", "6.000", "12,5"),
                    ("75.000", "1.500", "50")):
        assert f"{a} ÷ {b} = **{q}**" in theory, (a, b, q)
        assert float(a.replace(".", "")) / float(b.replace(".", "")) == float(q.replace(",", "."))


def test_relative_links_in_owned_docs_resolve():
    link = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
    broken = []
    for p in OWNED:
        for target in link.findall(_text(p)):
            if "://" in target or target.startswith("mailto:"):
                continue
            if not (p.parent / target).exists():
                broken.append(f"{p.name}: {target}")
    assert not broken, broken
