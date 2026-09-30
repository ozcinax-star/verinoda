"""`verinoda health` and the ``health`` concern of `verinoda review`: complexity, nesting, length and parameters
per function, a health score over thresholds, and near-duplicate functions with a similarity score.

The review tests run on small generated git repositories (never on examples/)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import health as hl  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

PY = '''
def f(a, b, c):
    if a and b or c:
        for x in a:
            if x:
                return 1
    elif b:
        pass
    else:
        pass
    return [y for y in b if y]


class K:
    def m(self, x):
        def inner(z):
            while z:
                z -= 1
        try:
            return x
        except ValueError:
            return None
'''


def _ts_or_skip(rel: str, text: str) -> dict:
    res = hl.file_metrics(rel, text)
    if res is None:
        pytest.skip("no tree-sitter grammar for " + rel)
    return res


def test_python_metrics_are_counted_per_function():
    ms = hl.file_metrics("a.py", PY)
    f = ms["f"]
    # 1 + if + (and, or) + for + if + elif + comprehension (for, if)
    assert f.cyclomatic == 9
    # if 1, or 1, and 1, for 1+1, if 1+2, elif 1, else 1, comprehension 1+0 and its if 1
    assert f.cognitive == 12
    assert (f.nesting, f.deepest, f.params, f.lines) == (3, 5, 3, 10)
    # a nested function is its own entry, not part of its parent's counts; self is not a parameter
    assert set(ms) == {"f", "K.m", "K.m.inner"}
    assert (ms["K.m"].cyclomatic, ms["K.m"].cognitive, ms["K.m"].params) == (2, 1, 1)
    assert (ms["K.m.inner"].cyclomatic, ms["K.m.inner"].nesting) == (2, 1)
    d = f.as_dict()
    assert d["symbol"] == "a.py::f" and d["at"] == "a.py:2-11" and d["deepest_at"] == "a.py:5"
    assert d["metrics_status"] == "statically_verified" and d["health_status"] == "strong_inference"


def test_javascript_and_java_metrics_from_the_tree():
    js = ("function g(a, b) {\n  if (a && b) {\n    for (const x of a) {\n"
          "      if (x) { return 1; } else if (b) { return 2; } else { return 3; }\n    }\n  }\n"
          "  return a ? 1 : 2;\n}\n")
    g = _ts_or_skip("a.js", js)["g"]
    assert g.values() == {"cyclomatic": 7, "cognitive": 10, "nesting": 3, "lines": 8, "params": 2}
    java = ("class A {\n  int h(int a, int b) {\n    switch (a) { case 1: return 1; case 2: return 2; "
            "default: return 0; }\n  }\n  void k(int x) { try { if (x > 0 || x < -1) {} } "
            "catch (Exception e) {} }\n}\n")
    ms = _ts_or_skip("A.java", java)
    assert (ms["A.h"].cyclomatic, ms["A.h"].cognitive) == (3, 1)   # `default` is no branch
    assert (ms["A.k"].cyclomatic, ms["A.k"].cognitive, ms["A.k"].nesting) == (4, 3, 1)


def test_unsupported_or_broken_text_is_none():
    assert hl.file_metrics("a.txt", "hello") is None
    assert hl.file_metrics("a.py", "def f(:\n") is None


def test_score_takes_a_point_per_threshold_reached():
    m = hl.FnMetrics("a.py", "f", 1, 200, cyclomatic=21, cognitive=3, nesting=4, params=2)
    hl.score(m)
    # cyclomatic 21 reaches 10 and 20, nesting 4 reaches 4, 200 lines reach 70 and 150
    assert m.health == 5
    assert [s["smell"] for s in m.smells] == ["complex_method", "deep_nesting", "long_method"]
    assert m.smells[0]["threshold"] == 20
    assert hl.band(m.health) == "problematic" and hl.band(10) == "healthy" and hl.band(1) == "unhealthy"
    worst = hl.score(hl.FnMetrics("a.py", "g", 1, 400, cyclomatic=40, cognitive=50, nesting=7, params=13))
    assert worst.health == 1


def _body(name: str, var: str) -> str:
    lines = [f"def {name}(items, limit):", f"    {var} = []"]
    for k in range(6):
        lines += ["    for it in items:", f"        if it > limit + {k}:", f"            {var}.append(it * {k})"]
    return "\n".join(lines + [f"    return {var}", ""])


def test_near_duplicates_ignore_names_and_literals():
    text = _body("one", "out") + "\n\n" + _body("two", "acc") + "\n\ndef small():\n    return 1\n"
    ms = hl.file_metrics("c.py", text, with_tokens=True)
    pairs, stats = hl.clones(list(ms.values()), min_tokens=30)
    assert len(pairs) == 1 and stats["truncated"] is False
    p = pairs[0]
    assert {p["a"]["symbol"], p["b"]["symbol"]} == {"c.py::one", "c.py::two"}
    assert p["similarity"] == 1.0 and p["status"] == "strong_inference" and p["tokens"] >= 30
    [(other, r)] = hl.near_duplicates(ms, "one", min_tokens=30)
    assert other.qual == "two" and r == 1.0
    assert hl.near_duplicates(ms, "small") == []   # below min_tokens


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    return repo


def test_report_ranks_by_health_and_lists_clones(tmp_path):
    deep = ("def deep(a):\n" + "".join("    " * (i + 1) + f"if a > {i}:\n" for i in range(6))
            + "    " * 7 + "return a\n    return 0\n")
    repo = _repo(tmp_path, {"pkg/m.py": deep + "\n\n" + _body("one", "out") + "\n\n" + _body("two", "acc"),
                            "tests/test_m.py": _body("test_x", "r"), "notes.txt": "x"})
    res = hl.report(repo, min_tokens=30)
    assert res["files"] == 1 and res["functions"] == 3   # tests are left out by default
    w = res["worst"][0]
    assert w["symbol"] == "pkg/m.py::deep" and w["nesting"] == 6 and w["health"] < 10
    assert "deep_nesting" in {s["smell"] for s in w["smells"]}
    assert res["clones_total"] == 1 and sum(res["bands"].values()) == 3
    text = hl.render_text(res)
    assert "pkg/m.py::deep" in text and "Near-duplicate functions: 1" in text
    only = hl.report(repo, ["tests"], with_clones=False, limit=1)
    assert only["functions"] == 1 and only["clones"] is None and only["worst_truncated"] is False
    assert hl.report(repo, ["nope"])["unmatched"] == ["nope"]
    with pytest.raises(ValueError):
        hl.report(repo, min_similarity=0)


def test_cli_health_json(tmp_path):
    repo = _repo(tmp_path, {"m.py": PY})
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-m", "verinoda", "health", "--repo", str(repo), "--json"],
                         capture_output=True, text=True, env=env, cwd=repo)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["functions"] == 3 and res["worst"][0]["symbol"] == "m.py::f"
    bad = subprocess.run([sys.executable, "-m", "verinoda", "health", "missing", "--repo", str(repo)],
                         capture_output=True, text=True, env=env, cwd=repo)
    assert bad.returncode == 2


# -- review ----------------------------------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


SIMPLE = '''
def handle(order, limit):
    if order > limit:
        return limit
    return order


def other(x):
    return x + 1
'''

TANGLED = '''
def handle(order, limit):
    if order > limit:
        for part in range(order):
            if part % 2:
                while part > limit:
                    if part and limit or order:
                        part -= 1
    return order


def other(x):
    return x + 1
'''


@pytest.fixture()
def indexed(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    from verinoda import workflow
    from verinoda.store import open_store

    repo = _repo(tmp_path, {"app/core.py": SIMPLE, ".gitignore": ".verinoda/\n__pycache__/\n"})
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return repo


def _review(repo: Path, **kw) -> dict:
    from verinoda import review as rv
    from verinoda.store import open_store

    st = open_store(repo)
    try:
        return rv.review(repo, store=st, concerns=["health"], record=False, **kw)
    finally:
        st.close()


def test_review_reports_a_health_drop_on_a_changed_function(indexed):
    (indexed / "app/core.py").write_text(TANGLED, encoding="utf-8", newline="\n")
    res = _review(indexed)
    [f] = [x for x in res["concerns"]["health"] if x["rule"] == "health-drop"]
    assert f["status"] == "strong_inference" and f["at"] == "app/core.py:2"
    assert f["metrics"]["base"]["health"] > f["metrics"]["head"]["health"]
    assert f["metrics"]["head"]["nesting"] == 5 and "deep nesting" in f["finding"]
    assert f["evidence_at"] == ["app/core.py:7"]   # the deepest branch


def test_review_reports_a_clone_and_a_low_health_function_it_adds(indexed):
    tangled_new = TANGLED.replace("def handle(", "def handle_again(").split("\n\ndef other")[0]
    body = _body("collect", "out")
    (indexed / "app/core.py").write_text(SIMPLE + "\n\n" + body + "\n\n" + _body("gather", "acc") + "\n\n"
                                         + tangled_new + "\n", encoding="utf-8", newline="\n")
    res = _review(indexed)
    rules = {(x["rule"], x.get("for", "")) for x in res["concerns"]["health"]}
    assert ("health-low-added", "app/core.py::handle_again") in rules
    clones = [x for x in res["concerns"]["health"] if x["rule"] == "clone-added"]
    assert len(clones) == 1 and clones[0]["similarity"] == 1.0   # one finding per pair
    assert not [x for x in res["concerns"]["health"] if x["rule"] == "health-drop"]


def test_review_of_an_unchanged_health_is_quiet_and_a_plan_is_not_measured(indexed):
    (indexed / "app/core.py").write_text(SIMPLE.replace("return x + 1", "return x + 2"), encoding="utf-8",
                                         newline="\n")
    assert _review(indexed)["concerns"]["health"] == []
    (indexed / "app/core.py").write_text(SIMPLE, encoding="utf-8", newline="\n")
    plan = _review(indexed, targets=["app/core.py::handle"], change="body")
    assert plan["concerns"]["health"] == []
    assert any(s.startswith("health") for s in plan["coverage"]["not_checked"])
