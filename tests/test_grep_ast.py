"""Structural search (verinoda/grep_ast.py): a code-shaped pattern with metavariables matched on the tree-sitter
trees of Python, Java and TypeScript files, each match statically_verified with its file:line and captures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinoda import cli, grep_ast

FILES = {
    "app/calc.py": """def run(x):
    foo(1, 2, 3)
    foo(x)  # one
    bar(foo(a, b))
    foo()
    same(x, x)
    same(x, y)
""",
    "app/Calc.java": """class Calc {
  int m(int a) {
    foo(1, "two");
    this.foo(3);
    foo();
    return a;
  }
}
""",
    "web/calc.ts": """function f(a: number) {
  foo(a, /* c */ 2);
  return foo(7);
}
""",
    "README.md": "foo(1, 2)\n",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _at(res: dict) -> list[str]:
    return [m["at"] for m in res["matches"]]


def test_a_pattern_is_found_across_python_java_and_typescript(repo):
    res = grep_ast.run(repo, "foo($A, $$$REST)")
    assert res["status"] == "found"
    assert _at(res) == ["app/Calc.java:3", "app/calc.py:2", "app/calc.py:3", "app/calc.py:4", "web/calc.ts:2",
                        "web/calc.ts:3"]
    by = {m["at"]: m for m in res["matches"]}
    assert by["app/calc.py:2"]["captures"] == {"A": "1", "REST": "2, 3"}
    assert by["app/calc.py:3"]["captures"] == {"A": "x", "REST": ""}       # $$$ matches zero nodes
    assert by["app/Calc.java:3"]["captures"] == {"A": "1", "REST": '"two"'}
    assert by["web/calc.ts:2"]["captures"] == {"A": "a", "REST": "2"}       # the comment is skipped
    assert {m["lang"] for m in res["matches"]} == {"python", "java", "typescript"}
    assert all(m["status"] == "statically_verified" for m in res["matches"])
    assert by["app/calc.py:4"]["text"] == "foo(a, b)"                        # a nested call is found
    assert res["by_lang"] == {"java": 1, "python": 1, "typescript": 1}       # the Markdown file is not read


def test_metavariables_agree_and_anonymous_ones_capture_nothing(repo):
    assert _at(grep_ast.run(repo, "same($A, $A)")) == ["app/calc.py:6"]
    res = grep_ast.run(repo, "same($_, $_)")
    assert _at(res) == ["app/calc.py:6", "app/calc.py:7"]
    assert all(m["captures"] == {} for m in res["matches"])
    assert len(grep_ast.run(repo, "foo($$$)")["matches"]) == 8   # not `this.foo(3)`: it has a receiver
    assert _at(grep_ast.run(repo, "$X.foo($A)")) == ["app/Calc.java:4"]


def test_a_language_whose_grammar_cannot_parse_the_pattern_is_named(repo):
    res = grep_ast.run(repo, "def $F($$$ARGS): $$$BODY")
    assert _at(res) == ["app/calc.py:1"]
    assert res["matches"][0]["captures"]["F"] == "run"
    assert sorted(res["not_searched"]["pattern_not_parsed"]["pattern"]) == ["java", "typescript"]
    # a Java statement is parsed inside a method body (Java has no top-level statements)
    assert _at(grep_ast.run(repo, "return $A", langs=["java"])) == ["app/Calc.java:6"]


def test_bad_patterns_and_languages_are_refused(repo):
    with pytest.raises(grep_ast.PatternError, match="only a metavariable"):
        grep_ast.run(repo, "$A")
    with pytest.raises(grep_ast.PatternError, match="unknown language"):
        grep_ast.run(repo, "foo($A)", langs=["cobol"])
    with pytest.raises(grep_ast.PatternError, match="does not parse"):
        grep_ast.run(repo, "def (((", langs=["python"])


def test_lang_and_paths_narrow_the_search(repo):
    assert {m["lang"] for m in grep_ast.run(repo, "foo($$$)", langs=["ts"])["matches"]} == {"typescript"}
    assert {m["at"].split(":")[0] for m in grep_ast.run(repo, "foo($$$)", paths=["app"])["matches"]} == {
        "app/calc.py", "app/Calc.java"}


def test_bounds_are_said(repo, monkeypatch):
    res = grep_ast.run(repo, "foo($$$)", file_seconds=0)
    assert res["count"] == 0 and len(res["not_searched"]["timed_out"]) == 3
    res = grep_ast.run(repo, "foo($$$)", max_results=2)
    assert res["truncated"] and res["count"] == 8 and len(res["matches"]) == 2
    monkeypatch.setattr(grep_ast, "MAX_BYTES", 10)
    assert grep_ast.run(repo, "foo($$$)")["not_searched"]["too_large"] == 3


def test_yaml_rule_files(repo):
    rules = repo / "rules.yml"
    rules.write_text("""# two rules
id: no-foo-pair
language: python
rule:
  pattern: |
    foo($A, $B)
message: "foo takes one argument"
---
id: java-return
language: Java
pattern: return $X
""", encoding="utf-8")
    loaded = grep_ast.load_rules(rules)
    assert [(r["id"], r["langs"], r["pattern"]) for r in loaded] == [
        ("no-foo-pair", ["python"], "foo($A, $B)"), ("java-return", ["java"], "return $X")]
    res = grep_ast.run(repo, rules=loaded)
    assert [(m["at"], m["rule"]) for m in res["matches"]] == [("app/Calc.java:6", "java-return"),
                                                              ("app/calc.py:4", "no-foo-pair")]
    assert res["matches"][1]["message"] == "foo takes one argument"
    rules.write_text("id: x\nmessage: no pattern\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no pattern"):
        grep_ast.load_rules(rules)


def test_cli_json_and_exit_codes(repo, capsys):
    assert cli.main(["grep-ast", "foo($A, $$$REST)", "web", "--repo", str(repo), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [m["at"] for m in out["matches"]] == ["web/calc.ts:2", "web/calc.ts:3"]
    assert cli.main(["grep-ast", "nothing_here($A)", "--repo", str(repo)]) == 1
    assert "0 match(es)" in capsys.readouterr().out
    assert cli.main(["grep-ast", "$A", "--repo", str(repo)]) == 2
    assert "only a metavariable" in capsys.readouterr().err
    assert cli.main(["grep-ast", "foo($A)", "missing/dir", "--repo", str(repo)]) == 2
    rules = repo / "r.yml"
    rules.write_text("id: r\nlanguage: typescript\npattern: foo($A)\n", encoding="utf-8")
    assert cli.main(["grep-ast", "--rule", str(rules), "web", "--repo", str(repo)]) == 0   # `web` is a path
    assert "[r] foo(7)" in capsys.readouterr().out
