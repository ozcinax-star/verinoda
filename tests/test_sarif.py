"""SARIF 2.1.0 in and out (verinoda.sarif): `review`, `check` and `decide check` written as SARIF, and a linter's or
CodeQL's SARIF read as evidence (`verinoda sarif`)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, workflow  # noqa: E402
from verinoda import sarif  # noqa: E402
from verinoda.store import open_store  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

# -- a subset of the SARIF 2.1.0 schema (the parts GitHub code scanning reads), checked without the network --

_STR = {"type": "string", "minLength": 1}
_LEVEL = {"enum": ["none", "note", "warning", "error"]}
_REGION = {"type": "object", "required": ["startLine"],
           "properties": {"startLine": {"type": "integer", "minimum": 1}, "endLine": {"type": "integer", "minimum": 1},
                          "startColumn": {"type": "integer", "minimum": 1}}}
_LOCATION = {"type": "object", "required": ["physicalLocation"], "properties": {"physicalLocation": {
    "type": "object", "required": ["artifactLocation"],
    "properties": {"artifactLocation": {"type": "object", "required": ["uri"],
                                        "properties": {"uri": _STR, "uriBaseId": _STR}},
                   "region": _REGION}}}}
_RESULT = {"type": "object", "required": ["ruleId", "message", "locations"],
           "properties": {"ruleId": _STR, "ruleIndex": {"type": "integer", "minimum": 0}, "level": _LEVEL,
                          "message": {"type": "object", "required": ["text"], "properties": {"text": _STR}},
                          "locations": {"type": "array", "minItems": 1, "items": _LOCATION},
                          "suppressions": {"type": "array", "items": {
                              "type": "object", "required": ["kind"],
                              "properties": {"kind": {"enum": ["inSource", "external"]}}}},
                          "baselineState": {"enum": ["new", "unchanged", "updated", "absent"]},
                          "properties": {"type": "object"}}}
_RULE = {"type": "object", "required": ["id"],
         "properties": {"id": _STR, "shortDescription": {"type": "object", "required": ["text"],
                                                         "properties": {"text": _STR}}}}
SCHEMA = {"type": "object", "required": ["version", "runs"], "properties": {
    "$schema": _STR, "version": {"enum": ["2.1.0"]},
    "runs": {"type": "array", "minItems": 1, "items": {
        "type": "object", "required": ["tool", "results"],
        "properties": {"tool": {"type": "object", "required": ["driver"], "properties": {"driver": {
            "type": "object", "required": ["name"],
            "properties": {"name": _STR, "version": _STR, "rules": {"type": "array", "items": _RULE}}}}},
            "automationDetails": {"type": "object", "properties": {"id": _STR}},
            "results": {"type": "array", "items": _RESULT}}}}}}
_TYPES = {"object": dict, "array": list, "string": str, "integer": int}


def _validate(doc, schema=SCHEMA, at="$") -> list[str]:
    """Errors of ``doc`` against the subset of JSON Schema used above (type, required, properties, items, enum,
    minimum, minLength, minItems)."""
    errs = []
    t = schema.get("type")
    if t and (not isinstance(doc, _TYPES[t]) or (t == "integer" and isinstance(doc, bool))):
        return [f"{at}: not {t}"]
    if "enum" in schema and doc not in schema["enum"]:
        errs.append(f"{at}: {doc!r} not in {schema['enum']}")
    if "minimum" in schema and doc < schema["minimum"]:
        errs.append(f"{at}: below {schema['minimum']}")
    if "minLength" in schema and len(doc) < schema["minLength"]:
        errs.append(f"{at}: empty")
    if "minItems" in schema and len(doc) < schema["minItems"]:
        errs.append(f"{at}: fewer than {schema['minItems']} items")
    for k in schema.get("required", []):
        if k not in doc:
            errs.append(f"{at}: lacks {k}")
    for k, sub in (schema.get("properties") or {}).items():
        if isinstance(doc, dict) and k in doc:
            errs += _validate(doc[k], sub, f"{at}.{k}")
    if "items" in schema:
        for i, x in enumerate(doc):
            errs += _validate(x, schema["items"], f"{at}[{i}]")
    return errs


def _valid(log: dict) -> dict:
    errs = _validate(log)
    assert not errs, errs
    run = log["runs"][0]
    rules = run["tool"]["driver"]["rules"]
    assert len({r["id"] for r in rules}) == len(rules)
    for r in run["results"]:   # ruleIndex points at the rule, uris are relative posix paths
        assert rules[r["ruleIndex"]]["id"] == r["ruleId"]
        uri = r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        assert "\\" not in uri and not uri.startswith("/") and ":" not in uri
    json.dumps(log)
    return run


def test_the_validator_rejects_a_broken_log():
    assert _validate({"version": "2.0.0", "runs": []})
    assert _validate({"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "x"}}, "results": [
        {"ruleId": "r", "message": {"text": "m"}, "level": "fatal",
         "locations": [{"physicalLocation": {"artifactLocation": {"uri": "a.py"}, "region": {"startLine": 0}}}]}]}]})


# -- out --------------------------------------------------------------------------------------------------------

REVIEW = {"exit": 3, "concerns": {
    "security": [{"concern": "security", "rule": "subprocess", "finding": "subprocess.run on a changed line",
                  "status": "statically_verified", "at": "app/run.py:12", "evidence_at": ["app/run.py:3"],
                  "basis": "the call is bound through imports", "derived_by": "verinoda.review_rules"}],
    "performance": [{"concern": "performance", "rule": "io-in-loop", "finding": "open() inside a loop",
                     "status": "weak_inference", "at": "app/io.py:4-6"}],
    "public_api": [{"concern": "public_api", "rule": "removed", "finding": "f removed", "status": "strong_inference",
                    "at": "app/x.py::f"}]},
    "tests": {"coverage": {"uncovered": [{"at": "app/io.py:5", "status": "strong_inference", "finding": "line 5 ran "
                                          "under no test", "evidence_at": ["app/io.py:5", "coverage.xml"]}]}}}


def test_review_findings_become_results_at_their_lines_a_warning_at_most():
    run = _valid(sarif.export(REVIEW, "review"))
    by = {r["ruleId"]: r for r in run["results"]}
    sec = by["review/security/subprocess"]
    assert sec["level"] == "warning"   # verified, but a review finding is something to read, not a defect
    assert sec["properties"]["status"] == "statically_verified"
    assert sec["properties"]["evidence_at"] == ["app/run.py:3"]
    assert sec["locations"][0]["physicalLocation"]["region"] == {"startLine": 12}
    io = by["review/performance/io-in-loop"]
    assert io["level"] == "note" and io["locations"][0]["physicalLocation"]["region"] == {"startLine": 4,
                                                                                          "endLine": 6}
    assert by["review/tests/uncovered-changed-lines"]["level"] == "warning"
    # a finding at a symbol, not a file line, is not written but counted
    assert "review/public_api/removed" not in by and run["properties"]["not_exported"] == 1
    assert run["invocations"][0]["exitCode"] == 3
    assert run["automationDetails"]["id"] == "verinoda/review/"


DECIDE = {"exit": 1, "base": {"ref": "HEAD", "commit": "a" * 40, "changed_files": 1},
          "violations": [{"decision": "D1", "guard": "g1", "kind": "only_in", "level": "VIOLATED",
                          "at": "src/db.py:7", "why": "sqlite3 used outside store/", "status": "statically_verified",
                          "since": "new/touched since HEAD", "what": "sqlite3"}],
          "possible": [{"decision": "D1", "guard": "g1", "kind": "only_in", "level": "POSSIBLE", "at": "src/x.py:2",
                        "why": "importlib names it", "status": "weak_inference"}],
          "pre_existing": [{"decision": "D1", "guard": "g1", "kind": "only_in", "level": "VIOLATED",
                            "at": "src/old.py:1", "why": "w", "status": "statically_verified"}],
          "baselined": [{"decision": "D2", "guard": "g", "kind": "no_edge", "level": "VIOLATED", "at": "a.py:3",
                         "why": "w", "status": "statically_verified"}],
          "baseline": {"file": "docs/decisions/baseline.json", "recorded": "2026-01-01"},
          "waived": [{"decision": "D3", "guard": "g", "kind": "only_in", "level": "VIOLATED", "at": "b.py:9",
                      "why": "w", "status": "statically_verified", "waiver": {"reason": "migration", "until": "2027"}}],
          "reviews": [], "triggers": [{"decision": "D4", "guard": "t", "why": "no location"}]}


def test_decide_check_violations_are_errors_and_baselined_or_waived_ones_suppressed():
    run = _valid(sarif.export(DECIDE, "decide check"))
    res = run["results"]
    at = {r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]: r for r in res}
    assert at["src/db.py"]["level"] == "error" and at["src/db.py"]["baselineState"] == "new"
    assert at["src/db.py"]["ruleId"] == "decide/D1/g1" and "VIOLATED D1 g1" in at["src/db.py"]["message"]["text"]
    assert at["src/x.py"]["level"] == "note" and at["src/x.py"]["properties"]["verdict"] == "POSSIBLE"
    assert at["src/old.py"]["baselineState"] == "unchanged" and "suppressions" not in at["src/old.py"]
    assert at["a.py"]["suppressions"][0]["kind"] == "external" and "baseline" in \
        at["a.py"]["suppressions"][0]["justification"]
    assert "migration" in at["b.py"]["suppressions"][0]["justification"]
    assert len(res) == 5   # the trigger has no location


CHECK = {"exit": 3, "sites": [
    {"at": "m.py:3:8", "kind": "attribute", "expr": "os.pathh", "verdict": "absent",
     "message": "not found in os as installed in .venv"},
    {"at": "m.py:1:1", "kind": "import", "expr": "requestz", "verdict": "not_installed", "why": "not installed"},
    {"at": "m.py:5:1", "kind": "attribute", "expr": "x.y", "verdict": "unknown", "rank": "high", "why": "open"},
    {"at": "m.py:6:1", "kind": "import", "expr": "tomllib", "verdict": "guarded"}]}


def test_check_sites_map_verdicts_to_levels_with_columns():
    run = _valid(sarif.export(CHECK, "check"))
    lv = {r["ruleId"]: r for r in run["results"]}
    assert set(lv) == {"check/absent/attribute", "check/not_installed/import", "check/unknown/attribute"}
    absent = lv["check/absent/attribute"]
    assert absent["level"] == "error" and absent["properties"]["status"] == "statically_verified"
    assert absent["locations"][0]["physicalLocation"]["region"] == {"startLine": 3, "startColumn": 8}
    assert lv["check/not_installed/import"]["level"] == "warning"
    assert lv["check/unknown/attribute"]["level"] == "note"


def test_a_path_with_spaces_and_non_ascii_is_a_valid_uri():
    run = _valid(sarif.export({"sites": [{"at": "my dir/ğ.py:2:1", "kind": "import", "expr": "x",
                                          "verdict": "absent"}]}, "check"))
    uri = run["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "my%20dir/%C4%9F.py"


def test_cli_check_sarif_prints_a_valid_log_with_the_same_exit(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    (tmp_path / "m.py").write_text("import os\n\nprint(os.getcwd())\n", encoding="utf-8")
    code = cli.main(["check", "--repo", str(tmp_path), "m.py", "--env", "none", "--sarif", "--no-cache"])
    log = json.loads(capsys.readouterr().out)
    run = _valid(log)
    assert run["invocations"][0].get("exitCode") == code


# -- in ---------------------------------------------------------------------------------------------------------

PY = """import os


def load(path):
    x = 1
    return open(path).read()
"""


def _sarif_file(path: Path, runs: list[dict]) -> Path:
    path.write_text(json.dumps({"version": "2.1.0", "runs": runs}), encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj ğ"
    (r / "src/app").mkdir(parents=True)
    (r / "src/app/io.py").write_text(PY, encoding="utf-8")
    (r / "other.py").write_text("y = 2\n", encoding="utf-8")
    old = time.time() - 100
    for f in ("src/app/io.py", "other.py"):
        os.utime(r / f, (old, old))
    return r


RUFF = {"tool": {"driver": {"name": "ruff", "version": "0.6.0", "rules": [
    {"id": "F841", "shortDescription": {"text": "unused variable"}}]}},
        "results": [
    {"ruleId": "F841", "level": "error", "message": {"text": "Local variable `x` is assigned to but never used"},
     "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/app/io.py"}, "region": {"startLine": 5}}}]},
    {"ruleId": "F401", "message": {"text": "`os` imported but unused"}, "suppressions": [{"kind": "inSource"}],
     "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/app/io.py"}, "region": {"startLine": 1}}}]},
    {"ruleId": "E501", "kind": "pass", "message": {"text": "ok"}},
    {"ruleId": "F999", "message": {"text": "elsewhere"},
     "locations": [{"physicalLocation": {"artifactLocation": {"uri": "/nowhere/else.py"}}}]},
]}

CODEQL = {"tool": {"driver": {"name": "CodeQL", "rules": [
    {"id": "py/path-injection", "defaultConfiguration": {"level": "warning"},
     "messageStrings": {"default": {"text": "This path depends on {0}."}}}]}},
          "originalUriBaseIds": {"%SRCROOT%": {"uri": "file:///home/runner/work/proj/proj/"}},
          "artifacts": [{"location": {"uri": "src/app/io.py", "uriBaseId": "%SRCROOT%"}}],
          "results": [
    {"ruleIndex": 0, "message": {"id": "default", "arguments": ["a user-provided value"]},
     "locations": [{"physicalLocation": {"artifactLocation": {"index": 0, "uriBaseId": "%SRCROOT%"},
                                         "region": {"startLine": 6, "startColumn": 12}}}]},
    {"ruleId": "py/path-injection", "message": {"text": "fixed"}, "baselineState": "absent",
     "locations": [{"physicalLocation": {"artifactLocation": {"uri": "other.py"}}}]},
    {"ruleId": "py/x", "message": {"text": "logical only"},
     "locations": [{"logicalLocations": [{"fullyQualifiedName": "load"}]}]}]}


def test_linter_and_codeql_results_are_the_tools_statements_at_their_lines(tmp_path):
    r = _repo(tmp_path)
    _sarif_file(r / "ruff.sarif", [RUFF])
    _sarif_file(r / "codeql.sarif", [CODEQL])
    res = sarif.report(r, ["ruff.sarif", "codeql.sarif"])
    by = {c["subject"]: c for c in res["claims"]}
    ruff = by["ruff/F841"]
    assert ruff["at"] == "src/app/io.py:5" and ruff["status"] == "strong_inference" and ruff["level"] == "error"
    assert ruff["stated_by"] == "ruff" and ruff["symbol"] == "load"
    assert ruff["evidence_at"] == ["src/app/io.py:5", "ruff.sarif"]
    assert "not checked" in ruff["derived_by"]
    cq = by["CodeQL/py/path-injection"]   # rule by index, message from messageStrings, file through the artifact
    assert cq["at"] == "src/app/io.py:6" and cq["level"] == "warning"
    assert "This path depends on a user-provided value." in cq["claim"]
    s = res["summary"]
    assert (s["suppressed"], s["passed"], s["absent"], s["no_location"], s["not_in_repository"]) == (1, 1, 1, 1, 1)
    assert res["not_in_repository"] == ["/nowhere/else.py"]
    assert [c["level"] for c in res["claims"]] == ["error", "warning"]   # errors first
    assert {x["file"] for x in res["sarif"]} == {"ruff.sarif", "codeql.sarif"}


def test_a_file_changed_after_the_sarif_was_written_is_weak(tmp_path):
    r = _repo(tmp_path)
    p = _sarif_file(r / "ruff.sarif", [RUFF])
    old = time.time() - 50
    os.utime(p, (old, old))
    (r / "src/app/io.py").write_text(PY + "\n", encoding="utf-8")
    c = sarif.report(r, [str(p)])["claims"][0]
    assert c["status"] == "weak_inference" and "may have moved" in c["claim"]


def test_an_absolute_ci_path_is_tied_by_its_longest_suffix_and_paths_filter(tmp_path):
    r = _repo(tmp_path)
    run = {"tool": {"driver": {"name": "eslint"}}, "results": [
        {"ruleId": "no-unused", "message": {"text": "m"}, "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "file:///home/runner/work/proj/proj/src/app/io.py"},
            "region": {"startLine": 4}}}]},
        {"ruleId": "no-unused", "message": {"text": "m"}, "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "other.py"}, "region": {"startLine": 1}}}]}]}
    _sarif_file(r / "e.sarif", [run])
    res = sarif.report(r, ["e.sarif"], ["src"])
    assert [c["at"] for c in res["claims"]] == ["src/app/io.py:4"]
    assert res["summary"]["outside_paths"] == 1


def test_an_exported_log_reads_back_as_verinodas_statements(tmp_path):
    r = _repo(tmp_path)
    log = sarif.export({"sites": [{"at": "src/app/io.py:6:12", "kind": "attribute", "expr": "f.read",
                                   "verdict": "absent"}]}, "check")
    (r / "v.sarif").write_text(sarif.dumps(log), encoding="utf-8")
    c = sarif.report(r, ["v.sarif"])["claims"][0]
    assert c["stated_by"] == "Verinoda" and c["rule"] == "check/absent/attribute" and c["at"] == "src/app/io.py:6"


def test_not_sarif_and_broken_runs_are_errors_not_crashes(tmp_path):
    r = _repo(tmp_path)
    (r / "bad.sarif").write_text("{\"version\": \"1.0\"}", encoding="utf-8")
    (r / "junk.sarif").write_text("not json", encoding="utf-8")
    _sarif_file(r / "odd.sarif", [{"tool": "x", "results": [{"ruleId": 1, "locations": "nope"}]}])
    res = sarif.report(r, ["bad.sarif", "junk.sarif", "odd.sarif", "missing.sarif"])
    errs = {x["file"]: x.get("error") for x in res["sarif"]}
    assert "not a SARIF 2.1 log" in errs["bad.sarif"] and errs["junk.sarif"] and errs["missing.sarif"]
    assert res["claims"] == []


def test_cli_sarif_json_and_no_file_read(tmp_path, capsys):
    r = _repo(tmp_path)
    assert cli.main(["sarif", "--repo", str(r), "nope.sarif"]) == 4
    assert "no SARIF file read" in capsys.readouterr().err
    _sarif_file(r / "ruff.sarif", [RUFF])
    assert cli.main(["sarif", "--repo", str(r), "ruff.sarif", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["claims"][0]["subject"] == "ruff/F841"
    assert cli.main(["sarif", "--repo", str(r), "ruff.sarif"]) == 0
    text = capsys.readouterr().out
    assert "[strong_inference] ruff reports F841" in text and "not checked by Verinoda" in text


# -- a real review ----------------------------------------------------------------------------------------------

SHOP = """import subprocess


def total(items):
    s = 0
    for i in items:
        s += i
    return s


def report(items):
    return total(items)
"""


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Me", "GIT_AUTHOR_EMAIL": "me@example.org",
           "GIT_COMMITTER_NAME": "Me", "GIT_COMMITTER_EMAIL": "me@example.org"}
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main", "-c",
                           "commit.gpgsign=false", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()


@needs_git
def test_cli_review_sarif_is_a_valid_log_with_the_reviews_exit(tmp_path, capsys):
    r = tmp_path / "shop repo"
    r.mkdir()
    (r / "cart.py").write_bytes(SHOP.encode("utf-8"))
    (r / ".gitignore").write_bytes(b".verinoda/\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "cart")
    workflow.init(r)
    st = open_store(r)
    try:
        workflow.scan(st, r)
    finally:
        st.close()
    (r / "cart.py").write_bytes(SHOP.replace("s += i", "s += i\n        subprocess.run(['ls'])").encode("utf-8"))
    code = cli.main(["review", "--repo", str(r), "--json"])
    res = json.loads(capsys.readouterr().out)
    assert cli.main(["review", "--repo", str(r), "--sarif"]) == code
    run = _valid(json.loads(capsys.readouterr().out))
    n = sum(len(v) for v in res["concerns"].values())
    assert len(run["results"]) + run["properties"]["not_exported"] == n
    assert n >= 1 and all(x["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "cart.py"
                          for x in run["results"])
    assert all(x["level"] in ("warning", "note") for x in run["results"])
