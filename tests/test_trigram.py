"""The trigram index of verinoda search."""

from __future__ import annotations

import os
import re
import subprocess
import time

import pytest

from verinoda import trigram
from verinoda.cli import main


def _lits(node) -> set[str]:
    if node is None:
        return set()
    if node[0] == "lit":
        return {node[1]}
    return set().union(*(_lits(x) for x in node[1]))


def test_required_literals():
    assert trigram.required("def changes_vs_base") == ("lit", "def changes_vs_base")
    r = trigram.required(r"class \w+Error\b")
    assert r[0] == "and" and _lits(r) == {"class ", "Error"}
    assert trigram.required("foo|barbaz") == ("or", [("lit", "foo"), ("lit", "barbaz")])
    assert trigram.required("fo|barbaz") is None          # one alternative requires nothing
    assert trigram.required(r"\w+") is None
    assert trigram.required("(abc)?xyz") == ("lit", "xyz")  # an optional group requires nothing
    assert trigram.required("(abcd)+") == ("lit", "abcd")
    assert trigram.required("ab") is None


def test_case_folding_letters_break_literals():
    # under IGNORECASE, i/k/s match non-ASCII letters (İ, ı, the Kelvin sign, the long s): never required
    assert _lits(trigram.required("parking lot", ignore_case=True)) == {"par", "ng lot"}
    assert _lits(trigram.required("(?i)parking lot")) == {"par", "ng lot"}
    assert _lits(trigram.required("şehirde", ignore_case=True)) == {"rde"}
    assert trigram.required("şehir") == ("lit", "şehir")
    for word, text in (("parking", "PARKİNG"), ("ok", "oK"), ("bus", "buſ")):
        assert re.search(word, text, re.I)  # why: these match, so the index must not rule them out


def _git(cwd, *a):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj ş"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.py").write_text("def apply_discount(x):\n    return x * 0.9\n", encoding="utf-8")
    (r / "src" / "b.py").write_text("PARKİNG = 1\nclass OrderError(Exception):\n    pass\n", encoding="utf-8")
    (r / "notes.md").write_text("Şehirde sipariş verildi\n", encoding="utf-8")
    (r / "blob.bin").write_bytes(b"\0apply_discount")
    (r / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (r / "ignored.txt").write_text("apply_discount\n", encoding="utf-8")
    _git(r, "init", "-q")
    return r


def _walk(repo, pattern, flags=0):
    rx = re.compile(pattern, flags | re.M)
    out = set()
    for rel in trigram._list_files(repo):
        data = trigram._read(repo, rel)
        if data is None:
            continue
        text = data.decode("utf-8", "replace")
        for m in rx.finditer(text):
            out.add(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
    return out


@pytest.mark.parametrize("pattern,ic", [("apply_discount", False), (r"class \w+Error", False),
                                        ("parking", True), ("şehirde", True), ("sipariş", False),
                                        (r"x \* 0\.9|pass", False), (r"^\s+return", False), (r"\w+", False)])
def test_search_equals_a_walk(repo, pattern, ic):
    res = trigram.search(repo, pattern, ignore_case=ic, max_results=10_000)
    assert {m["at"] for m in res["matches"]} == _walk(repo, pattern, re.I if ic else 0)
    assert res["status"] == "observed"


def test_ignored_binary_and_scope(repo):
    res = trigram.search(repo, "apply_discount")
    assert [m["at"] for m in res["matches"]] == ["src/a.py:1"]       # not the binary, not the ignored file
    assert trigram.search(repo, "apply_discount", paths=["notes.md"])["total"] == 0
    assert trigram.search(repo, "apply_discount", fixed=True, paths=["src/"])["total"] == 1


def test_narrowing_and_incremental_update(repo):
    first = trigram.search(repo, "apply_discount")
    assert first["narrowed"] and first["candidates"] == 1 and first["files_indexed"] == 5
    p = repo / "src" / "b.py"
    p.write_text("def apply_discount_twice(x):\n    pass\n", encoding="utf-8")
    os.utime(p, ns=(time.time_ns(), time.time_ns() + 10_000_000))
    (repo / "src" / "a.py").unlink()
    (repo / "src" / "c.py").write_text("apply_discount(1)\n", encoding="utf-8")
    res = trigram.search(repo, "apply_discount")
    assert {m["at"] for m in res["matches"]} == {"src/b.py:1", "src/c.py:1"}
    assert res["index_update"]["removed"] == 1 and res["index_update"]["changed"] == 2
    again = trigram.search(repo, "PARKİNG")   # the old content of b.py is gone from the postings
    assert again["total"] == 0 and again["candidates"] == 0
    full = trigram.refresh(repo, rebuild=True)
    assert full["built"] and full["indexed"] == 5


def test_cli_exit_codes(repo, capsys):
    assert main(["search", "apply_discount", "--repo", str(repo)]) == 0
    assert "src/a.py:1:" in capsys.readouterr().out
    assert main(["search", "nothing_like_this", "--repo", str(repo), "--json"]) == 1
    assert main(["search", "(", "--repo", str(repo)]) == 2
    assert main(["search", "", "--repo", str(repo)]) == 2


def test_pathological_patterns_stay_bounded(repo):
    t = time.perf_counter()
    trigram.required("(" * 90 + "a" + ")" * 90)
    trigram.required("a" * 1999)
    assert time.perf_counter() - t < 2
    with pytest.raises(ValueError):
        trigram.search(repo, "a" * 2001)
