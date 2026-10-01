"""Mutation testing scoped to the diff (verinoda mutate)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from verinoda import mutate
from verinoda.cli import main
from verinoda.store import open_store


def test_mutants_only_on_changed_lines_and_never_in_docstrings_or_comments():
    src = ('def f(a, b):\n'
           '    """1 < 2"""\n'
           '    if a < b and not a:  # x < y\n'
           '        return a + 1\n'
           '    return b * 2 >= 3\n')
    ms = mutate.mutants_for("m.py", src, {3})
    assert ms and {m["line"] for m in ms} == {3}
    ops = {m["op"] for m in ms}
    assert {"< -> <=", "and -> or", "not x -> x", "condition negated"} <= ops
    for m in ms:  # the rest of the file is byte for byte the same
        assert m["text"][:m["start"]] == src[:m["start"]]
        assert '"""1 < 2"""' in m["text"] and "# x < y" in m["text"]
        compile(m["text"], "m.py", "exec")


def test_operators_and_dedup():
    src = "x = a is not None\ny = (n // 2) - 1\nz = True\n"
    ops = [(m["line"], m["op"]) for m in mutate.mutants_for("m.py", src, {1, 2, 3})]
    assert (1, "is not -> is") in ops
    assert (2, "// -> *") in ops and (2, "- -> +") in ops and (2, "2 -> 3") in ops
    assert (3, "True -> False") in ops
    assert len({m["text"] for m in mutate.mutants_for("m.py", src, {1, 2, 3})}) == len(ops)


def test_non_ascii_offsets_and_unparseable_text():
    src = 'def f(ş):\n    return "ğü" + ş\n'
    ms = mutate.mutants_for("m.py", src, {2})
    plus = next(m for m in ms if m["op"] == "+ -> -")
    assert plus["text"] == 'def f(ş):\n    return "ğü" - ş\n'
    assert mutate.mutants_for("m.py", "def f(:\n", {1}) == []


def test_changed_lines():
    assert mutate.changed_lines(b"a\nb\nc\n", b"a\nB\nc\nd\n") == {2, 4}
    assert mutate.changed_lines(None, b"a\nb") == {1, 2}


def _git(cwd, *a):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def shop(tmp_path, monkeypatch):
    repo = tmp_path / "shop ş"
    (repo / "shop").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "shop" / "p.py").write_text("def price(n, vip):\n    return n\n", encoding="utf-8")
    (repo / "tests" / "test_p.py").write_text(
        "from shop.p import price\n\n\ndef test_price():\n    assert price(10, False) == 10\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    monkeypatch.setattr("verinoda.experiments.is_trusted", lambda r: True)
    monkeypatch.setattr("verinoda.experiments.python_for", lambda r: sys.executable)
    return repo


def test_surviving_mutants_are_reported_with_their_line(shop):
    (shop / "shop" / "p.py").write_text(
        "def price(n, vip):\n    if vip:\n        return n - 1\n    return n\n", encoding="utf-8")
    st = open_store(shop, create=True)
    try:
        res = mutate.run(st, shop, tests=["tests/test_p.py::test_price"], max_mutants=3)
    finally:
        st.close()
    assert res["baseline"]["outcome"] == "pass"
    by = {(m["at"], m["op"]): m for m in res["mutants"]}
    assert by[("shop/p.py:2", "condition negated")]["result"] == "killed"
    surv = [m for m in res["mutants"] if m["result"] == "survived"]
    assert surv and all(m["at"] == "shop/p.py:3" and m["experiment"] for m in surv)
    assert all(m["claim"]["status"] == "strong_inference" for m in surv)
    assert res["status"] == "survivors"
    assert res["not_run_over_limit"] == res["mutants_total"] - 3


def test_a_failing_baseline_runs_no_mutant(shop):
    (shop / "shop" / "p.py").write_text("def price(n, vip):\n    return n + 1\n", encoding="utf-8")
    st = open_store(shop, create=True)
    try:
        res = mutate.run(st, shop, tests=["tests/test_p.py::test_price"])
    finally:
        st.close()
    assert res["status"] == "baseline_failed" and res["mutants"] == []


def test_nothing_to_mutate_and_cli_exit_codes(shop, capsys):
    assert main(["mutate", "--repo", str(shop), "--json"]) == 0
    assert '"nothing_to_mutate"' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["mutate", "--repo", str(shop), "--max-mutants", "-1"])
    (shop / "shop" / "p.py").write_text("def price(n, vip):\n    return n if vip else n\n", encoding="utf-8")
    assert main(["mutate", "--repo", str(shop), "--tests", "tests/test_p.py::test_price", "--max-mutants", "0",
                 "--json"]) == 3   # mutants exist but none was run: no pass
