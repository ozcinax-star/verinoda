"""Mermaid diagrams and the wiki outline (verinoda.diagrams) on a scanned copy of examples/orders_app."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import diagrams, index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

STEERING = {
    "pages": [
        {"title": "Orders app", "purpose": "What the app does.", "overview": True},
        {"title": "HTTP layer", "parent": "Orders app", "purpose": "The handlers.", "paths": ["orders/api.py"],
         "flows": [["create_order_handler", "OrderRepository.save"], ["no_such_handler_zz", "save"], ["one"]]},
        {"title": "Domain", "parent": "Orders app", "paths": ["orders/service.py", "orders/pricing.py"]},
        {"title": "Storage", "parent": "Domain", "paths": ["orders/repo*"]},
        {"title": "Ghost", "parent": "Nobody", "paths": ["nothing/"]},
    ]
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _scanned(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    return _scanned(tmp_path_factory.mktemp("diagrams") / "orders_app")


@pytest.fixture(scope="module")
def g(repo):
    return index.load(repo)


@pytest.fixture
def steered(repo):
    p = repo / diagrams.STEERING_FILE
    p.write_text(json.dumps(STEERING), encoding="utf-8")
    yield repo
    p.unlink()


def _cited_lines_exist(repo: Path, claims: list[dict]) -> None:
    for c in claims:
        assert c["status"] in ("strong_inference", "weak_inference")  # an edge is never a verification
        assert c["evidence"], c
        for ev in c["evidence"]:
            f, _, line = ev.rpartition(":")
            assert line.isdigit() and 1 <= int(line) <= len((repo / f).read_text(encoding="utf-8").splitlines())


def test_architecture_counts_edges_between_parts_with_their_lines(repo, g):
    d = diagrams.diagram(g, "architecture")
    assert d["status"] == "found" and d["mermaid"].startswith("flowchart LR")
    boxes = dict(re.findall(r'^  (n\d+)\["([^"]+)"\]$', d["mermaid"], re.M))
    assert set(boxes.values()) >= {"orders", "tests"}
    by = {(c["from"], c["to"]): c for c in d["claims"]}
    tests_orders = by[("tests", "orders")]
    assert tests_orders["count"] > 0 and tests_orders["status"] == "strong_inference"
    ids = {v: k for k, v in boxes.items()}
    assert f"{ids['tests']} -->|{tests_orders['count']}| {ids['orders']}" in d["mermaid"]
    assert ("orders", "tests") not in by  # the code does not use its tests
    _cited_lines_exist(repo, d["claims"])
    assert d["coverage"]["limits"]


def test_flow_between_two_symbols_marks_an_inferred_call(repo, g):
    d = diagrams.diagram(g, "flow", "create_order_handler", "OrderRepository.save")
    assert d["status"] == "found" and d["mermaid"].startswith("flowchart TD")
    assert "OrderRepository.save()" in d["mermaid"] and "orders/service.py:19" in d["mermaid"]
    texts = {c["text"]: c for c in d["claims"]}
    assert texts["create_order_handler() calls place_order()"]["status"] == "strong_inference"
    inferred = texts["place_order() calls OrderRepository.save()"]
    assert inferred["status"] == "weak_inference" and inferred["evidence"] == ["orders/service.py:22"]
    assert re.search(r"n\d+ -\.->\|calls\| n\d+", d["mermaid"])  # the inferred call is dashed
    _cited_lines_exist(repo, d["claims"])


def test_flow_of_one_symbol_is_what_it_calls(g):
    d = diagrams.flow(g, "place_order")
    assert d["status"] == "found" and d["title"] == "what place_order() calls"
    assert {"place_order() calls validate_items()", "place_order() calls compute_total()"} <= \
        {c["text"] for c in d["claims"]}


def test_sequence_is_messages_between_owners(g):
    d = diagrams.diagram(g, "sequence", "create_order_handler", "OrderRepository.save")
    lines = d["mermaid"].splitlines()
    assert lines[0] == "sequenceDiagram"
    parts = dict(re.findall(r"participant (p\d+) as (.+)$", d["mermaid"], re.M))
    assert set(parts.values()) == {"api.py", "service.py", "OrderRepository"}
    ids = {v: k for k, v in parts.items()}
    assert f"  {ids['api.py']}->>{ids['service.py']}: place_order()" in lines
    assert f"  {ids['service.py']}-){ids['OrderRepository']}: save()" in lines  # inferred: an open arrow
    assert [c["status"] for c in d["claims"]] == ["strong_inference", "weak_inference"]


def test_a_name_that_names_nothing_gives_no_diagram(g):
    for d in (diagrams.flow(g, "no_such_handler_zz", "save"), diagrams.sequence(g, "no_such_handler_zz", "save"),
              diagrams.flow(g, "no_such_handler_zz")):
        assert d["status"] == "unresolved" and d["mermaid"] is None and d["claims"] == [] and d["next_step"]
    with pytest.raises(ValueError):
        diagrams.diagram(g, "sequence", "place_order")
    with pytest.raises(ValueError):
        diagrams.diagram(g, "pie")


def test_labels_cannot_break_the_diagram():
    s = diagrams._text('a "b"; #c <d> |e|\nf')
    assert not set(s) & set('"<>|\n')
    assert s == "a #quot;b#quot;#59; #35;c #lt;d#gt; #124;e#124; f"


def test_as_text_is_a_mermaid_file_with_its_evidence_as_comments(g):
    text = diagrams.as_text(diagrams.diagram(g, "flow", "create_order_handler", "OrderRepository.save"))
    lines = text.splitlines()
    head = lines[:lines.index("flowchart TD")]
    assert head and all(line.startswith("%% ") for line in head)
    assert any("weak_inference: place_order() calls OrderRepository.save() at orders/service.py:22" in x for x in head)


def test_default_outline_is_an_overview_and_a_page_per_folder(repo, g):
    o = diagrams.outline(g, repo, diagrams=False)
    assert o["source"].startswith("default")
    pages = {p["id"]: p for p in o["pages"]}
    assert o["pages"][0]["id"] == "overview" and o["pages"][0]["diagram_kinds"] == ["architecture"]
    assert pages["orders"]["parent"] == "overview" and pages["orders"]["depth"] == 1
    assert "orders/service.py" in pages["orders"]["files"] and pages["orders"]["diagram_kinds"] == ["files"]
    assert not any("diagrams" in p for p in o["pages"]) and o["unassigned_files"] == 0
    one = diagrams.outline(g, repo, pages=["orders"])
    got = {p["id"]: p for p in one["pages"]}
    assert "diagrams" not in got["overview"] and got["orders"]["diagrams"][0]["kind"] == "files"
    files = got["orders"]["diagrams"][0]
    assert '(["tests"])' in files["mermaid"]  # another page is a rounded box
    assert any(c["text"].startswith("orders/api.py uses orders/service.py") for c in files["claims"])
    _cited_lines_exist(repo, files["claims"])


def test_a_repo_file_steers_the_pages(steered):
    g = index.load(steered)
    o = diagrams.outline(g, steered, stale=[diagrams.STEERING_FILE])  # as freshness reports the new file
    assert o["source"] == diagrams.STEERING_FILE
    assert [(p["id"], p["depth"], p["parent"]) for p in o["pages"]] == [
        ("orders-app", 0, None), ("http-layer", 1, "orders-app"), ("domain", 1, "orders-app"),
        ("storage", 2, "domain"), ("ghost", 0, None)]
    pages = {p["id"]: p for p in o["pages"]}
    assert pages["storage"]["files"] == ["orders/repository.py"]  # a glob
    arch = pages["orders-app"]["diagrams"][0]
    assert arch["kind"] == "architecture" and {c["text"].split(" (")[0] for c in arch["claims"]} >= \
        {"HTTP layer uses Domain", "Domain uses Storage"}
    http = pages["http-layer"]["diagrams"]
    assert [d["kind"] for d in http] == ["files", "flow", "sequence", "flow", "sequence"]
    # a name spelled in the steering file is not a name of a changed code file: it resolves (or not) as indexed
    assert http[1]["status"] == "found" and http[2]["status"] == "found"
    assert http[3]["status"] == "unresolved" and http[3]["mermaid"] is None
    problems = "\n".join(o["problems"])
    assert "Ghost: path 'nothing/' matches no indexed file" in problems
    assert "parent 'Nobody' is no page's title" in problems
    assert "flow ['one'] is not" in problems and "flow no_such_handler_zz -> save: unresolved" in problems
    assert o["unassigned_files"] > 0  # tests, docs and config are in no page
    md = diagrams.as_markdown(o)
    assert md.startswith("# Orders app\n") and "## HTTP layer" in md and "### Storage" in md
    assert md.count("```mermaid") == 6 and "**Sequence: create_order_handler() -> OrderRepository.save()**" in md


def test_a_broken_steering_file_falls_back_and_says_so(repo, g):
    p = repo / diagrams.STEERING_FILE
    p.write_text("{not json", encoding="utf-8")
    try:
        o = diagrams.outline(g, repo, diagrams=False)
    finally:
        p.unlink()
    assert o["source"].startswith("default") and "not read" in o["problems"][0]


def test_cli_diagram_and_wiki(repo, capsys):
    from verinoda import cli

    assert cli.main(["diagram", "sequence", "create_order_handler", "OrderRepository.save", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("%% ") and "\nsequenceDiagram\n" in out
    assert cli.main(["diagram", "flow", "place_order", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "flow"
    assert cli.main(["diagram", "flow", "no_such_handler_zz", "save", "--repo", str(repo)]) == 2
    assert "no diagram: unresolved" in capsys.readouterr().out
    assert cli.main(["wiki", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "Overview  [overview]" in out and "    orders  [orders]" in out
    assert cli.main(["wiki", "--repo", str(repo), "--page", "orders"]) == 0
    assert "## orders" in capsys.readouterr().out
    assert cli.main(["wiki", "--repo", str(repo), "--json"]) == 0
    assert all("diagrams" in p for p in json.loads(capsys.readouterr().out)["pages"])


def test_mcp_map_view_outline(repo):
    from verinoda.mcp.server import AtlasTools

    tools = AtlasTools(repo)
    tree = tools.map_view("outline")
    assert tree["pages"][0]["id"] == "overview" and not any("diagrams" in p for p in tree["pages"])
    assert "targets" in tree["next_step"]
    one = tools.map_view("outline", targets=["overview"])
    page = next(p for p in one["pages"] if p["id"] == "overview")
    assert page["diagrams"][0]["mermaid"].startswith("flowchart LR") and "note" not in one


def test_the_html_export_carries_the_diagrams(repo, tmp_path):
    from verinoda.ui import export

    out = export.write(repo, tmp_path / "graph.html")
    assert out["wiki_pages"] >= 4 and out["diagrams"] >= 4
    html = Path(out["path"]).read_text(encoding="utf-8")
    raw = re.search(r'<script type="application/json" id="verinoda-data">(.*?)</script>', html, re.DOTALL).group(1)
    wiki = json.loads(raw)["wiki"]
    first = wiki["pages"][0]["diagrams"][0]
    assert first["kind"] == "architecture" and first["mermaid"].startswith("flowchart LR") and first["claims"]
    assert "<br/>" not in raw  # a diagram's markup is escaped in the embedded JSON
    assert '"#/w/"' in html and 'case "/api/wiki"' in html  # the page shows the wiki pages


# -- a small repository: two files named utils.py, a dot-folder, nested pages -------------------------

SMALL = {
    "a/__init__.py": "",
    "a/utils.py": "from b.utils import helper\n\n\ndef start():\n    return helper()\n",
    "b/__init__.py": "",
    "b/utils.py": "def finish():\n    return 1\n\n\ndef helper():\n    return finish()\n",
    "tools/run.py": "from a.utils import start\n\n\ndef main():\n    return start()\n",
    ".ci/check.py": "from tools.run import main\n\n\ndef check():\n    return main()\n",
}


@pytest.fixture(scope="module")
def small(tmp_path_factory) -> Path:
    dst = tmp_path_factory.mktemp("diagrams") / "small ğ repo"
    for rel, text in SMALL.items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text(text, encoding="utf-8")
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst


def _steer(repo: Path, data, bom: bool = False) -> dict:
    p = repo / diagrams.STEERING_FILE
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + json.dumps(data).encode("utf-8"))
    try:
        return diagrams.outline(index.load(repo), repo)
    finally:
        p.unlink()


def test_two_files_with_one_name_are_two_participants(small):
    d = diagrams.sequence(index.load(small), "main", "finish")
    parts = dict(re.findall(r"participant (p\d+) as (.+)$", d["mermaid"], re.M))
    assert set(parts.values()) == {"run.py", "a/utils.py", "b/utils.py"}
    ids = {v: k for k, v in parts.items()}
    assert f"{ids['a/utils.py']}->>{ids['b/utils.py']}" in d["mermaid"]  # not a call of utils.py to itself
    assert f"{ids['b/utils.py']}->>{ids['b/utils.py']}: finish()" in d["mermaid"]  # a call within one file


def test_a_steering_path_may_name_a_dot_folder(small):
    assert diagrams._matches(".ci/check.py", ".ci/") and diagrams._matches(".ci/check.py", "./.ci")
    assert diagrams._matches(".ci/check.py", "././.ci/*.py") and not diagrams._matches("ci/check.py", ".ci/")
    o = _steer(small, {"pages": [{"title": "CI", "paths": [".ci/"]}]})
    assert o["pages"][0]["files"] == [".ci/check.py"] and "problems" not in o


def test_nested_pages_draw_the_arrows_between_the_sub_pages(small):
    o = _steer(small, {"pages": [
        {"title": "All", "paths": ["a/", "b/", "tools/"]},
        {"title": "A", "parent": "All", "paths": ["a/"]},
        {"title": "B", "parent": "All", "paths": ["b/"]},
        {"title": "Tools", "parent": "All", "paths": ["tools/*.py"]}]})
    arch = o["pages"][0]["diagrams"][0]
    assert arch["kind"] == "architecture"
    pairs = {(c["from"], c["to"]) for c in arch["claims"]}
    assert {("tools", "a"), ("a", "b")} <= pairs  # a file counts for the sub-page that lists it
    assert '"All"' not in arch["mermaid"]  # every file of All is in a sub-page


def test_a_mistyped_steering_file_is_listed_under_problems(small, tmp_path):
    from verinoda.ui import export

    bad = {"pages": [{"title": "A", "parent": ["x"], "paths": 5, "flows": 5},
                     {"title": "B", "parent": {"a": 1}, "paths": ["a/", 3], "flows": "ab"},
                     {"title": "A", "paths": ["b/"]}, {"title": ["t"]}]}
    o = _steer(small, bad, bom=True)  # a BOM is read, not an error
    assert o["source"] == diagrams.STEERING_FILE
    assert [p["id"] for p in o["pages"]] == ["a", "b"] and all(p["depth"] == 0 for p in o["pages"])
    problems = "\n".join(o["problems"])
    for text in ("A: parent ['x'] is not a page's title", "A: paths 5 is not a list", "A: flows 5 is not a list",
                 "B: parent {'a': 1}", "B: paths [3] are not text", "B: flows 'ab' is not a list",
                 "pages[2]: title 'A' is an earlier page's title", "pages[3]: no title"):
        assert text in problems, text
    assert o["pages"][1]["files"] == ["a/__init__.py", "a/utils.py"]
    p = small / diagrams.STEERING_FILE
    p.write_text(json.dumps(bad), encoding="utf-8")
    try:
        assert export.write(small, tmp_path / "graph.html")["wiki_pages"] == 2
    finally:
        p.unlink()


def test_a_flow_cut_to_its_boxes_says_how_many_were_left_out(g, monkeypatch):
    monkeypatch.setattr(diagrams, "MAX_NODES", 2)
    d = diagrams.flow(g, "place_order")
    assert d["truncated"] and d["left_out"]["boxes"] > 0 and d["left_out"]["arrows"] > 0
    assert "%% truncated: {}" not in diagrams.as_text(d)


def test_a_flow_of_a_symbol_that_calls_nothing_says_so(repo, g, capsys):
    from verinoda import cli

    d = diagrams.flow(g, "orders/api.py")
    assert d["status"] == "no outgoing calls" and d["mermaid"] is None and d["next_step"]
    assert cli.main(["diagram", "flow", "orders/api.py", "--repo", str(repo)]) == 2
    assert "no diagram: no outgoing calls" in capsys.readouterr().out


def test_mcp_outline_with_targets_gives_only_those_pages(repo):
    from verinoda.mcp.server import AtlasTools

    one = AtlasTools(repo).map_view("outline", targets=["tests"])
    assert [p["id"] for p in one["pages"]] == ["tests"] and one["pages"][0]["diagrams"]
    assert one["page_count"] > 1 and "truncated" not in one


def test_the_export_escapes_a_flow_diagrams_markup(steered, tmp_path):
    from verinoda.ui import export

    out = export.write(steered, tmp_path / "graph.html")
    html = Path(out["path"]).read_text(encoding="utf-8")
    raw = re.search(r'<script type="application/json" id="verinoda-data">(.*?)</script>', html, re.DOTALL).group(1)
    http = next(p for p in json.loads(raw)["wiki"]["pages"] if p["id"] == "http-layer")
    assert "<br/><small>" in http["diagrams"][1]["mermaid"]  # a flow's box: a name and its line
    assert "<br/>" not in raw and "</small>" not in raw
