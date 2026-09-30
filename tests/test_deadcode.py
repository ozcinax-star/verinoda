"""The map's dead view (verinoda.deadcode): unreached code as claims, each naming what was searched and the
dynamic uses that could keep it alive."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import deadcode, index, map_text, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

FILES = {
    "pyproject.toml": '[project]\nname = "shapes"\nversion = "0.1"\n\n[project.scripts]\nshapes = "pkg.tooling:start"\n',
    "pkg/__init__.py": '"""Shapes."""\n',
    "pkg/core.py": '''"""Core."""
import threading


def used():
    return helper()


def helper():
    return 1


def lonely():
    return chain()


def chain():
    return 2


def by_string():
    return after_string()


def after_string():
    return 3


def register(fn):
    return fn


@register
def registered():
    return 4


def outer():
    def inner():
        return 5
    return inner()


def only_tested():
    return 6


class Shape:
    def __repr__(self):
        return "Shape"

    def unused_method(self):
        return 7


class Worker(threading.Thread):
    def run(self):
        return 8


def make():
    Worker()
    return Shape()


def pytest_configure(config):
    return config
''',
    "pkg/orphan.py": '"""Nothing imports this."""\n\n\ndef forgotten():\n    return 9\n',
    "pkg/tooling.py": '"""A console script."""\n\n\ndef start():\n    return 10\n',
    "run.py": '''from pkg.core import make, outer, used

REGISTRY = ["by_string"]


def main():
    print(make(), outer(), used())


if __name__ == "__main__":
    main()
''',
    "tests/test_core.py": '''from pkg.core import only_tested


def test_only_tested():
    assert only_tested() == 6
''',
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _scan(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    r = tmp_path_factory.mktemp("dead") / "shapes"
    for rel, text in FILES.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text, encoding="utf-8")
    _scan(r)
    return r


@pytest.fixture(scope="module")
def view(repo) -> dict:
    return deadcode.dead_code(index.load(repo))


def _claim(view: dict, subject: str) -> dict | None:
    return next((c for c in view["claims"] if c["subject"] == subject), None)


def test_every_claim_names_what_was_searched_and_its_dynamic_uses(view):
    assert view["view"] == "dead" and view["coverage"]["method"] and view["coverage"]["limits"]
    assert view["claims"]
    for c in view["claims"]:
        assert c["status"] in ("strong_inference", "weak_inference")  # a heuristic: never verified
        assert c["status"] == ("weak_inference" if c["dynamic_uses"] else "strong_inference")
        assert "entry points" in c["entry_points_searched"] and c["entry_points_searched"] in c["claim"]
        assert c["evidence_at"][0] == c["at"] and ":" in c["at"]
        for u in c["dynamic_uses"]:
            assert u["kind"] and u["at"] and u["why"]
    s = view["searched"]
    assert any(e["symbol"] == "main()" for e in s["entry_points"])
    assert {"file": "run.py", "why": "__main__ guard: run as a script"} in s["entry_modules"]
    assert s["test_code_nodes"] > 0
    json.dumps(view)


def test_orphan_file_is_one_claim_with_its_symbols_counted(view):
    c = _claim(view, "pkg/orphan.py")
    assert c["kind"] == "orphan_file" and c["status"] == "strong_inference" and c["members"] == 1
    assert _claim(view, "forgotten()") is None  # counted under its file, not repeated
    assert view["claims"][0]["kind"] == "orphan_file"


def test_zero_callers_and_unreached_callers(view):
    lonely = _claim(view, "lonely()")
    assert lonely["kind"] == "zero_callers" and lonely["status"] == "strong_inference" and not lonely["callers"]
    chain = _claim(view, "chain()")
    assert chain["kind"] == "callers_unreached" and chain["callers"][0]["from"] == "lonely()"
    assert chain["callers"][0]["at"] == "pkg/core.py:14" and "pkg/core.py:14" in chain["evidence_at"]
    assert chain["status"] == "strong_inference"  # the known caller's own call site is no dynamic use
    method = _claim(view, ".unused_method()")
    assert method["symbol_kind"] == "method" and method["status"] == "strong_inference"


def test_reached_code_is_not_claimed(view):
    subjects = {c["subject"] for c in view["claims"]}
    # called from an entry point, from an entry module's import, only from a test, nested in a reached
    # function, a dunder of a reached class, a declared console script, the package __init__
    for name in ("main()", "used()", "helper()", "make()", "outer()", "inner()", "only_tested()", "Shape",
                 ".__repr__()", "start()", "pkg/tooling.py", "pkg/__init__.py", "run.py"):
        assert name not in subjects, name
    assert any(e["symbol"] == "start()" and e.get("basis") == "declared" for e in view["searched"]["entry_points"])


def test_dynamic_uses_keep_a_claim_weak(view):
    s = _claim(view, "by_string()")
    assert s["status"] == "weak_inference"
    assert s["dynamic_uses"][0]["kind"] == "string" and s["dynamic_uses"][0]["at"] == "run.py:3"
    after = _claim(view, "after_string()")  # reached from it: kept alive with it
    assert after["status"] == "weak_inference" and after["dynamic_uses"][0]["kind"] == "via"
    reg = _claim(view, "registered()")
    assert reg["status"] == "weak_inference" and any(u["kind"] == "decorator" and "@register" in u["why"]
                                                    for u in reg["dynamic_uses"])
    hook = _claim(view, "pytest_configure()")
    assert hook["status"] == "weak_inference" and hook["dynamic_uses"][0]["kind"] == "convention"
    run = _claim(view, ".run()")
    assert run["status"] == "weak_inference" and any(u["kind"] == "override" and "Thread" in u["why"]
                                                    for u in run["dynamic_uses"])


def test_limit_truncates_and_says_so(repo):
    small = deadcode.dead_code(index.load(repo), limit=2)
    assert len(small["claims"]) == 2 and small["truncated"] and small["claims_not_shown"] > 0


def test_text_rendering_is_bounded(view):
    text = map_text.render({"dead": view}, 4)
    assert "dead-code claims" in text and "strong_inference" in text and "more claims" in text


def test_orders_app_dead_view(tmp_path):
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    _scan(repo)
    v = am.VIEWS["dead"](index.load(repo))
    assert [c["subject"] for c in v["claims"]] == ["load_settings()"]  # defined, never called
    assert v["claims"][0]["status"] == "strong_inference"
    env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
               PYTHONIOENCODING="utf-8", GRAPHIFY_OUT=".verinoda/index")
    r = subprocess.run([sys.executable, "-m", "verinoda", "map", str(repo), "--view", "dead", "--json"], cwd=repo,
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
                       stdin=subprocess.DEVNULL, check=False)
    assert r.returncode == 0, r.stderr[-2000:]
    assert [c["subject"] for c in json.loads(r.stdout)["dead"]["claims"]] == ["load_settings()"]
    from verinoda.mcp.server import AtlasTools

    res = AtlasTools(repo).map_view("dead")
    assert res["view"] == "dead" and res["claims"][0]["subject"] == "load_settings()"
