"""Coverage reports read into lines and symbols (verinoda.coverage_import), `verinoda coverage` and the review's
changed lines no test ran, for Python, Java and TypeScript."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import coverage_import as ci  # noqa: E402
from verinoda import review as rv  # noqa: E402
from verinoda import workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

JAVA = """package com.ex;

public class Calc {
    public int add(int a, int b) {
        return a + b;
    }

    public int sub(int a, int b) {
        return a - b;
    }
}
"""

TS = """export function double(x: number): number {
  return x * 2;
}

export function half(x: number): number {
  return x / 2;
}
"""

PY = """def area(w, h):
    return w * h


def perimeter(w, h):
    return 2 * (w + h)


def describe(w, h):
    return f"{area(w, h)} {perimeter(w, h)}"
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _repo(tmp_path: Path, files: dict[str, str], *, scan: bool = False) -> Path:
    repo = tmp_path / "proj"
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    if scan:
        workflow.init(repo)
        st = open_store(repo)
        try:
            workflow.scan(st, repo)
        finally:
            st.close()
    return repo


def _edit(repo: Path, rel: str, find: str, replace: str) -> None:
    p = repo / rel
    text = p.read_text(encoding="utf-8")
    assert text.count(find) == 1, (rel, find)
    p.write_text(text.replace(find, replace, 1), encoding="utf-8", newline="\n")


def _fresh(path: Path) -> None:
    """A report written after every file of the tree (file systems with a coarse clock)."""
    t = time.time() + 5
    os.utime(path, (t, t))


def _review(repo: Path, **kw) -> dict:
    st = open_store(repo)
    try:
        return rv.review(repo, store=st, **kw)
    finally:
        st.close()


# -- parsing ---------------------------------------------------------------------------------------------

def test_lcov_with_test_names_and_absolute_paths(tmp_path):
    root = tmp_path.resolve()
    data = (f"TN:unit\nSF:{(root / 'src' / 'a.ts').as_posix()}\nDA:1,2\nDA:2,0\nend_of_record\n"
            "TN:e2e\nSF:./src/a.ts\nDA:2,1\nDA:3,0\nend_of_record\n").encode()
    fmt, entries = ci.parse(data, root)
    assert fmt == "lcov"
    [(key, lines, tests)] = [e for e in entries if e[0] == "src/a.ts"]
    assert key == "src/a.ts" and lines == {1: 2, 2: 1, 3: 0}
    assert tests == {1: {"unit"}, 2: {"e2e"}}


def test_cobertura_joins_a_source_root(tmp_path):
    root = tmp_path.resolve()
    _write(root, "pkg/mod.py", "x = 1\n")
    data = (f"<?xml version='1.0'?><coverage><sources><source>{(root / 'pkg').as_posix()}</source></sources>"
            "<packages><package name='pkg'><classes><class name='mod.py' filename='mod.py'><methods/>"
            "<lines><line number='1' hits='3'/><line number='2' hits='0'/></lines></class></classes></package>"
            "</packages></coverage>").encode()
    fmt, [(key, lines, _t)] = ci.parse(data, root)
    assert (fmt, key, lines) == ("cobertura", "pkg/mod.py", {1: 3, 2: 0})


def test_jacoco_lines_and_a_suffix_match_to_the_source_tree(tmp_path):
    root = tmp_path.resolve()
    _write(root, "src/main/java/com/ex/Calc.java", JAVA)
    _write(root, "other/com/ex/Util.java", "class Util {}\n")
    _write(root, "copy/com/ex/Util.java", "class Util {}\n")
    report = root / "build/reports/jacoco/test/jacocoTestReport.xml"
    report.parent.mkdir(parents=True)
    report.write_text(
        '<?xml version="1.0"?><!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">'
        '<report name="t"><package name="com/ex"><sourcefile name="Calc.java">'
        '<line nr="5" mi="0" ci="3" mb="0" cb="0"/><line nr="9" mi="4" ci="0" mb="0" cb="0"/></sourcefile>'
        '<sourcefile name="Util.java"><line nr="1" mi="1" ci="0"/></sourcefile></package></report>',
        encoding="utf-8")
    cov = ci.load(root)
    assert [r["format"] for r in cov.read] == ["jacoco"]
    assert cov.resolve("src/main/java/com/ex/Calc.java") == ("com/ex/Calc.java", "suffix")
    assert cov.lines_of("src/main/java/com/ex/Calc.java").lines == {5: 3, 9: 0}
    assert cov.resolve("src/main/java/com/other/Calc.java") == (None, "absent")
    # one report entry, two repository files that fit it: each resolves to it, the command lists no ambiguity
    assert cov.resolve("other/com/ex/Util.java")[0] == "com/ex/Util.java"


def test_two_report_entries_that_fit_one_file_equally_are_ambiguous(tmp_path):
    cov = ci.Coverage(tmp_path)
    cov.files = {"a/x/Foo.java": ci.FileCov("a/x/Foo.java"), "b/x/Foo.java": ci.FileCov("b/x/Foo.java")}
    assert cov.resolve("x/Foo.java") == (None, "ambiguous")
    assert cov.resolve("src/a/x/Foo.java") == ("a/x/Foo.java", "suffix")


def test_coverage_py_json_contexts_name_the_tests(tmp_path):
    doc = {"meta": {"show_contexts": True}, "files": {"calc.py": {
        "executed_lines": [1, 2], "missing_lines": [5],
        "contexts": {"2": ["tests/test_calc.py::test_area|run"], "1": [""]}}}}
    fmt, [(key, lines, tests)] = ci.parse(json.dumps(doc).encode(), tmp_path)
    assert fmt == "coverage.py json" and key == "calc.py"
    assert lines == {1: 1, 2: 1, 5: 0} and tests == {2: {"tests/test_calc.py::test_area"}}


def test_an_unknown_file_is_refused_not_read_as_empty(tmp_path):
    with pytest.raises(ValueError):
        ci.parse(b"hello\n", tmp_path)
    with pytest.raises(ValueError):
        ci.parse(b"<html/>", tmp_path)


def test_ranges():
    assert ci.ranges({9, 3, 4, 5}) == "3-5, 9" and ci.ranges([]) == ""


# -- the command -------------------------------------------------------------------------------------------

def _cobertura(rel: str, lines: dict[int, int]) -> str:
    rows = "".join(f"<line number='{n}' hits='{h}'/>" for n, h in sorted(lines.items()))
    return (f"<?xml version='1.0'?><coverage><sources><source>.</source></sources><packages><package name='p'>"
            f"<classes><class name='c' filename='{rel}'><lines>{rows}</lines></class></classes></package></packages>"
            "</coverage>")


@needs_git
def test_report_per_symbol_with_status_and_evidence(tmp_path):
    repo = _repo(tmp_path, {"calc.py": PY})
    rp = repo / "coverage.xml"
    rp.write_text(_cobertura("calc.py", {1: 1, 2: 1, 5: 1, 6: 0}), encoding="utf-8")
    _fresh(rp)
    res = ci.report(repo)
    by = {c["subject"]: c for c in res["claims"]}
    per = by["calc.py::perimeter"]
    assert per["status"] == "strong_inference" and per["not_run"] == "6" and per["covered"] == 1
    assert per["at"] == "calc.py:5" and per["evidence_at"] == ["calc.py:5", "coverage.xml"]
    assert by["calc.py::area"]["not_run"] == ""
    assert res["claims"][0]["subject"] == "calc.py::perimeter"   # least covered first
    assert res["summary"]["percent"] == 75.0
    # the file changed after the report: the line numbers may have moved
    os.utime(rp, (1, 1))
    assert {c["status"] for c in ci.report(repo)["claims"]} == {"weak_inference"}


@needs_git
def test_indirect_coverage_changes_between_two_reports(tmp_path):
    repo = _repo(tmp_path, {"calc.py": PY, "other.py": "def f():\n    return 1\n"})
    (repo / "base.xml").write_text(
        _cobertura("calc.py", {2: 1, 6: 1}).replace("</classes>", "<class name='o' filename='other.py'><lines>"
                                                    "<line number='2' hits='1'/></lines></class></classes>"),
        encoding="utf-8")
    (repo / "head.xml").write_text(
        _cobertura("calc.py", {2: 1, 6: 0}).replace("</classes>", "<class name='o' filename='other.py'><lines>"
                                                    "<line number='2' hits='0'/></lines></class></classes>"),
        encoding="utf-8")
    _edit(repo, "calc.py", "return w * h", "return h * w")
    res = ci.report(repo, reports=["head.xml"], base_reports=["base.xml"])
    ch = res["changes"]
    assert [r["file"] for r in ch["indirect"]] == ["other.py"]
    assert ch["indirect"][0]["no_longer_run"] == "2" and ch["indirect"][0]["symbols_no_longer_run"] == ["f"]
    assert [r["file"] for r in ch["in_changed_files"]] == ["calc.py"]


@needs_git
def test_cli_coverage_json_and_no_report(tmp_path):
    repo = _repo(tmp_path, {"calc.py": PY})
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-m", "verinoda", "coverage", "--repo", str(repo), "--json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 4 and "no coverage report" in r.stderr
    (repo / "lcov.info").write_text("TN:\nSF:calc.py\nDA:2,1\nDA:6,0\nend_of_record\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "verinoda", "coverage", "--repo", str(repo), "--json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["reports"][0]["format"] == "lcov" and out["summary"]["covered_lines"] == 1


# -- review --------------------------------------------------------------------------------------------------

@needs_git
def test_review_names_changed_java_lines_no_test_covers(tmp_path):
    repo = _repo(tmp_path, {"src/main/java/com/ex/Calc.java": JAVA}, scan=True)
    _edit(repo, "src/main/java/com/ex/Calc.java", "        return a + b;\n",
          "        int s = a + b;\n        return s;\n")
    _edit(repo, "src/main/java/com/ex/Calc.java", "return a - b;", "return b - a;")
    rp = repo / "build/reports/jacoco/test/jacocoTestReport.xml"
    rp.parent.mkdir(parents=True)
    rp.write_text('<report name="t"><package name="com/ex"><sourcefile name="Calc.java">'
                  '<line nr="5" mi="0" ci="2"/><line nr="6" mi="0" ci="1"/><line nr="10" mi="3" ci="0"/>'
                  '</sourcefile></package></report>', encoding="utf-8")
    _fresh(rp)
    res = _review(repo)
    cv = res["tests"]["coverage"]
    assert cv["patch"] == {"measured_changed_lines": 3, "covered": 2, "percent": 66.7}
    [u] = cv["uncovered"]
    assert u["file"] == "src/main/java/com/ex/Calc.java" and u["lines"] == "10" and u["symbol"].endswith("sub")
    assert u["status"] == "strong_inference" and u["evidence_at"] == [
        "src/main/java/com/ex/Calc.java:10", "build/reports/jacoco/test/jacocoTestReport.xml"]
    assert res["exit"] == 3 and res["counts"]["uncovered_changed_lines"] == 1
    assert "changed lines no test covers: src/main/java/com/ex/Calc.java:10" in rv.render_text(res)
    assert rv.NO_LINE_COVERAGE not in res["coverage"]["limits"]


@needs_git
def test_review_names_changed_typescript_lines_no_test_covers(tmp_path):
    repo = _repo(tmp_path, {"src/math.ts": TS}, scan=True)
    _edit(repo, "src/math.ts", "return x / 2;", "return Math.floor(x / 2);")
    _edit(repo, "src/math.ts", "return x * 2;", "return x + x;")
    rp = repo / "coverage" / "lcov.info"
    rp.parent.mkdir()
    rp.write_text("TN:math doubles\nSF:src/math.ts\nDA:2,4\nDA:6,0\nend_of_record\n", encoding="utf-8")
    _fresh(rp)
    res = _review(repo)
    cv = res["tests"]["coverage"]
    assert [(u["file"], u["lines"], u["symbol"]) for u in cv["uncovered"]] == [("src/math.ts", "6",
                                                                               "src/math.ts::half")]
    assert cv["covered_by"] == {"src/math.ts::double": ["math doubles"]}
    # a changed symbol the report shows run is no longer "reached by no test"
    assert "src/math.ts::double" not in res["tests"]["no_test_reaches"]
    assert "src/math.ts::double" not in [r["symbol"] for r in res["tests"]["reach_unknown"]]


@needs_git
def test_review_with_a_stale_report_says_so_and_without_one_keeps_its_limit(tmp_path):
    repo = _repo(tmp_path, {"calc.py": PY}, scan=True)
    res = _review(repo, coverage_reports=None)
    assert "coverage" not in res["tests"] and rv.NO_LINE_COVERAGE in res["coverage"]["limits"]
    rp = repo / "coverage.xml"
    rp.write_text(_cobertura("calc.py", {2: 1, 6: 0}), encoding="utf-8")
    os.utime(rp, (1, 1))
    _edit(repo, "calc.py", "return 2 * (w + h)", "return (w + h) * 2")
    res = _review(repo)
    [u] = res["tests"]["coverage"]["uncovered"]
    assert u["status"] == "weak_inference" and "may have moved" in u["finding"]
    assert res["exit"] == 0 and not res["unknown"]   # a stale report's rows are not reason enough to exit 3
    # the report itself (untracked, not ignored) is not reviewed as a change
    assert [c["file"] for c in res["changes"]] == ["calc.py"]
    assert {"file": "coverage.xml", "why": "a coverage report"} in res["skipped"]
    _fresh(rp)
    res = _review(repo)
    assert res["tests"]["coverage"]["uncovered"][0]["status"] == "strong_inference" and res["exit"] == 3
    # a report asked for by name that cannot be read is an unknown
    (repo / "bad.xml").write_text("<nope/>", encoding="utf-8")
    res = _review(repo, coverage_reports=["bad.xml"])
    assert any(x["kind"] == "coverage_report" for x in res["unknown"]) and "coverage" not in res["tests"]
