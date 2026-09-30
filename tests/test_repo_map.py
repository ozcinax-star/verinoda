"""The repo view of the map: files ranked by PageRank toward the files in play, their signatures under a token
budget, on a scanned copy of examples/orders_app."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import cli, index, map_text, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _scan(repo: Path) -> index.Graph:
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()
    return index.load(repo)


@pytest.fixture(scope="module")
def app(tmp_path_factory) -> tuple[Path, index.Graph]:
    repo = tmp_path_factory.mktemp("repomap") / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    return repo, _scan(repo)


def _sig_files(v: dict) -> list[str]:
    return [r["file"] for r in v["files"]]


def test_the_map_ranks_files_and_quotes_each_signature_at_its_line(app):
    repo, g = app
    v = am.repo_map(g)
    assert v["view"] == "repo" and v["coverage"]["method"] and v["coverage"]["limits"]
    assert v["focus"] == [] and v["tokens"] <= v["max_tokens"] == am.REPO_MAP_TOKENS
    assert v["symbols_shown"] == v["symbols_total"]   # the example fits the default budget
    ranks = [r["rank"] for r in v["files"]]
    assert ranks == sorted(ranks, reverse=True) and all(r["status"] == "strong_inference" for r in v["files"])
    # what the others depend on comes before what depends on them
    files = _sig_files(v)
    assert files.index("orders/pricing.py") < files.index("orders/api.py")
    assert files.index("orders/service.py") < files.index("orders/api.py")
    for r in v["files"]:
        for s in r["signatures"]:
            f, ln = s["at"].rsplit(":", 1)
            assert f == r["file"]
            line = (repo / f).read_text(encoding="utf-8").splitlines()[int(ln) - 1]
            assert line.rstrip() == s["text"]
    assert "pyproject.toml" not in files   # a data file's keys are no signatures
    pricing = next(r for r in v["files"] if r["file"] == "orders/pricing.py")
    assert [s["text"] for s in pricing["signatures"]] == [
        "def compute_total(items: list[dict]) -> float:", "def apply_discount(subtotal: float) -> float:"]
    json.dumps(v)


def test_the_files_in_play_pull_the_rank_toward_what_they_use_and_are_left_out(app):
    _repo, g = app
    plain = {r["file"]: r["rank"] for r in am.repo_map(g)["files"]}
    v = am.repo_map(g, ["orders/api.py", "./orders\\api.py::create_order_handler", "nowhere.py"])
    ranked = {r["file"]: r["rank"] for r in v["files"]}
    assert v["focus"] == ["orders/api.py"] and "orders/api.py" not in ranked
    assert v["focus_unresolved"] == ["nowhere.py"] and v["focus_unresolved_total"] == 1
    assert ranked["orders/service.py"] > plain["orders/service.py"]


def test_the_budget_keeps_the_best_ranked_signatures_and_counts_what_it_rendered(app):
    _repo, g = app
    full = am.repo_map(g)
    small = am.repo_map(g, max_tokens=40)
    assert 0 < small["symbols_shown"] < full["symbols_shown"] and small["tokens"] <= 40
    rendered = [line for r in small["files"] for line in am.repo_map_lines(r)]
    assert sum(am.repo_tokens(line) for line in rendered) == small["tokens"]
    assert _sig_files(small)[0] == _sig_files(full)[0]
    assert am.repo_map(g, max_tokens=0)["files"] == []


def test_the_text_is_the_map_itself(app):
    _repo, g = app
    text = map_text.render({"repo": am.repo_map(g, ["orders/api.py"])}, 1000)
    assert "== repo ==" in text and "(strong_inference)" in text and "files in play (left out): orders/api.py" in text
    assert "orders/pricing.py:\n  L6 def compute_total(items: list[dict]) -> float:" in text


def test_cli_map_view_repo(app, capsys):
    repo, _g = app
    assert cli.main(["map", str(repo), "--view", "repo", "--max-tokens", "50", "--target", "orders/api.py",
                     "--json"]) == 0
    v = json.loads(capsys.readouterr().out)["repo"]
    assert v["max_tokens"] == 50 and v["tokens"] <= 50 and v["focus"] == ["orders/api.py"]
    assert cli.main(["map", str(repo), "--view", "repo"]) == 0
    out = capsys.readouterr().out
    assert "== repo ==" in out and "L6 def compute_total" in out


def test_mcp_map_view_repo(app):
    from verinoda.mcp.server import AtlasTools

    repo, _g = app
    res = AtlasTools(repo).map_view("repo", targets=["orders/service.py"])
    assert res["view"] == "repo" and res["focus"] == ["orders/service.py"] and res["focus_source"] == "argument"
    assert "orders/service.py" not in _sig_files(res) and res["files"]


def test_nested_functions_and_decorators(tmp_path):
    repo = tmp_path / "nested"
    repo.mkdir()
    (repo / "lib.py").write_text(
        "import functools\n\n\n"
        "class Box:\n    @functools.cache\n    def size(self) -> int:\n        return 1\n\n\n"
        "def outer(x):\n    def inner(y):\n        return y\n    return inner(x)\n", encoding="utf-8")
    (repo / "main.py").write_text("from lib import Box, outer\n\n\ndef run():\n    return outer(Box().size())\n",
                                  encoding="utf-8")
    v = am.repo_map(_scan(repo))
    lib = next(r for r in v["files"] if r["file"] == "lib.py")
    texts = [s["text"] for s in lib["signatures"]]
    assert "    def size(self) -> int:" in texts and "def outer(x):" in texts
    assert not any("inner" in t for t in texts) and not any(t.lstrip().startswith("@") for t in texts)
    assert next(s for s in lib["signatures"] if "size" in s["text"])["at"] == "lib.py:6"


def test_the_files_in_play_weigh_as_much_on_a_large_graph():
    # 900 files that all use one hub; the file in play uses one leaf: the leaf, not the hub, comes first
    nodes = [f"m{i}.py" for i in range(900)] + ["hub.py", "leaf.py", "play.py"]
    edges = {(f"m{i}.py", "hub.py"): 1.0 for i in range(900)}
    edges[("play.py", "leaf.py")] = 1.0
    plain = am._pagerank(nodes, edges, set())
    rank = am._pagerank(nodes, edges, {"play.py"})
    assert plain["hub.py"] > plain["leaf.py"]
    assert rank["leaf.py"] > rank["hub.py"] and max(rank, key=rank.get) in ("play.py", "leaf.py")
    assert abs(sum(rank.values()) - 1) < 1e-6


def test_targets_are_normalised_and_an_unknown_one_is_an_error_in_the_cli(app, capsys):
    repo, g = app
    v = am.repo_map(g, [str(repo / "orders" / "api.py"), r"orders\..\orders/service.py"])
    assert v["focus"] == ["orders/api.py", "orders/service.py"] and "focus_unresolved" not in v
    assert cli.main(["map", str(repo), "--view", "repo", "--target", "orders/api.pyy"]) == 2
    captured = capsys.readouterr()
    assert "not a file of the graph: orders/api.pyy" in captured.out and "error:" in captured.err


@pytest.mark.parametrize("budget", ["0", "-3"])
def test_cli_rejects_a_budget_below_one(app, capsys, budget):
    repo, _g = app
    assert cli.main(["map", str(repo), "--view", "repo", "--max-tokens", budget, "--json"]) == 2
    assert "--max-tokens must be at least 1" in capsys.readouterr().err


def test_mcp_lowers_the_budget_to_fit_the_cap_and_counts_what_it_sends(app):
    from verinoda.mcp.server import AtlasTools

    repo, _g = app
    res = AtlasTools(repo, max_chars=2400).map_view("repo", targets=["orders/api.py"])
    assert "truncation" not in res and res["budget_note"] and res["max_tokens"] < am.REPO_MAP_TOKENS
    assert res["symbols_shown"] == sum(len(r["signatures"]) for r in res["files"]) > 0
    assert all(set(s) == {"at", "text"} for r in res["files"] for s in r["signatures"])


def test_a_moved_definition_is_not_quoted_and_is_counted(tmp_path):
    repo = tmp_path / "stale"
    repo.mkdir()
    (repo / "a.py").write_text("import os\n\n\ndef get(x):\n    return x\n\n\ndef put(y):\n    return y\n",
                               encoding="utf-8")
    (repo / "b.py").write_text("from a import get, put\n\n\ndef run():\n    return get(put(1))\n", encoding="utf-8")
    g = _scan(repo)
    (repo / "a.py").write_text("import os\n\n\nbudget = 1\n\n\n\ndef put(y):\n    return y\n", encoding="utf-8")
    v = am.repo_map(g)
    texts = [s["text"] for r in v["files"] for s in r["signatures"]]
    assert "budget = 1" not in texts and "def put(y):" in texts
    assert v["signatures_not_found"] == 1 and v["symbols_total"] == v["symbols_shown"]
    assert "1 definition lines not found" in map_text.render({"repo": v}, 100)
