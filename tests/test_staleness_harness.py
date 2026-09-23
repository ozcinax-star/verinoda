"""The staleness harness (DESIGN D30): mutation suite and history replay against a from-scratch oracle."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.benchmark import staleness  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                       cwd=cwd, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return r.stdout


V1_UTIL = (
    "import os\n\n"
    "LIMIT = os.environ.get('APP_LIMIT', '5')\n\n\n"
    "def helper(x):\n    return x + 1\n\n\n"
    "def other(y):\n    return y * 2\n"
)
V1_MAIN = (
    "from pkg.util import helper\n\n\n"
    "def run(values):\n"
    "    total = 0\n"
    "    for v in values:\n"
    "        total += helper(v)\n"
    "    return total\n\n\n"
    "def idle():\n    return None\n"
)


@pytest.fixture
def history(tmp_path) -> Path:
    """A small repository whose commits exercise shifts, body edits, renames and rebinding."""
    repo = tmp_path / "corpus"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_bytes(b"")
    (repo / "pkg" / "util.py").write_bytes(V1_UTIL.encode())
    (repo / "pkg" / "main.py").write_bytes(V1_MAIN.encode())
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    steps = [
        ("pkg/main.py", V1_MAIN.replace("from pkg.util import helper\n", "from pkg.util import helper\n# note\n\n")),
        ("pkg/util.py", V1_UTIL.replace("return y * 2", "return y * 3")),
        ("pkg/main.py", None),  # rebinding helper to other (call line unchanged)
        ("pkg/util.py", None),  # rename helper -> assist everywhere
    ]
    for rel, text in steps:
        p = repo / rel
        cur = p.read_bytes().decode()
        if text is None and rel == "pkg/main.py":
            text = cur.replace("from pkg.util import helper\n", "from pkg.util import other as helper\n")
        elif text is None:
            text = cur.replace("def helper(", "def assist(")
            (repo / "pkg" / "main.py").write_bytes((repo / "pkg" / "main.py").read_bytes().replace(
                b"from pkg.util import other as helper", b"from pkg.util import assist as helper"))
        p.write_bytes(text.encode())
        _git(repo, "commit", "-q", "-am", f"edit {rel}")
    return repo


def test_derive_is_an_independent_from_scratch_population():
    claims, defs = staleness.derive("pkg/main.py", V1_MAIN, exists=lambda p: p == "pkg/util.py")
    kinds = sorted(c.kind for c in claims)
    assert kinds == ["location", "location", "relation"]
    rel = next(c for c in claims if c.kind == "relation")
    assert rel.target == ("pkg/util.py", "helper") and rel.start == 7 and rel.spec["at"] == "pkg/main.py:7"
    assert set(defs) == {"run", "idle"} and defs["run"]["start"] == 4
    util, _ = staleness.derive("pkg/util.py", V1_UTIL)
    assert any(c.kind == "config" and c.spec == {"env": "APP_LIMIT"} for c in util)
    shifted, _ = staleness.derive("pkg/main.py", "# c\n" + V1_MAIN, exists=lambda p: p == "pkg/util.py")
    assert {c.key for c in shifted} == {c.key for c in claims}  # keys carry no line numbers


def test_history_replay_recall_is_one_and_nothing_is_silently_wrong(history, tmp_path):
    res = staleness.replay(history, commits=10, pathspec="pkg/*.py", cap_per_kind=None, workdir=tmp_path / "w")
    assert res["commits_replayed"] == 4
    ov = res["overall"]
    assert ov["symbol_mode"]["recall"] == 1.0 and res["recall_ok"]
    assert res["silent_wrong"] == 0 and res["silent_wrong_ok"]
    assert ov["file_mode"]["recall"] == 1.0
    assert ov["symbol_mode"]["false_stale_rate"] < ov["file_mode"]["false_stale_rate"]
    assert ov["symbol_mode"]["stale"] < ov["file_mode"]["stale"]
    assert res["relocation"]["moved"] >= 2 and res["relocation"]["accuracy"] == 1.0
    assert res["rebound"] > 0 and res["invalidation_ms"]["p50"] is not None
    rel = res["by_kind"]["relation"]
    assert rel["truth_changed"] >= 2  # the rebinding and the rename commits


def test_mutation_suite_matches_its_expected_verdicts():
    res = staleness.run_mutation_suite()
    assert res["categories"] == len(staleness.MUTATIONS) == 12
    assert res["all_ok"], [r for r in res["rows"] if not r["ok"]]
    assert res["stale_recall"] == 1.0 and res["missed"] == []


def test_main_writes_results(tmp_path, history, capsys):
    out = tmp_path / "staleness.json"
    rc = staleness.main(["replay", "--repo", str(history), "--commits", "2", "--pathspec", "pkg/*.py", "--cap", "0",
                         "--out", str(out)])
    assert rc == 0 and out.is_file()
    assert b'"schema": "verinoda.staleness_replay/1"' in out.read_bytes() and b"\r\n" not in out.read_bytes()
