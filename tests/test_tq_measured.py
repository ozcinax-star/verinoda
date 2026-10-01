"""Measured frequencies for typed answers (verinoda/tq_measured.py, `verinoda benchmark tq-audit`): the second
frozen held-out set, the engine and gold hashes, the display rule (n >= 30, decided, dev inside the held-out
interval, both hashes current), the audit's cells and reliability table, and the committed calibration table."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, tq, tq_measured, workflow  # noqa: E402
from verinoda.benchmark import tq_audit  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLD2 = ROOT / "benchmarks" / "tq_gold2"
GIT = shutil.which("git") is not None


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def _scan(files: dict, dst: Path) -> Path:
    for rel, text in files.items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    (dst / ".gitignore").write_text(".verinoda/\n", encoding="utf-8")
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "one")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst


# -- the second held-out set ---------------------------------------------------------------------------------

def test_the_second_held_out_set_is_frozen_and_named_by_the_display_hash():
    manifest = json.loads((GOLD2 / "MANIFEST.json").read_text(encoding="utf-8"))
    data = (GOLD2 / "held_out.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["files"]["held_out.json"]
    rows = json.loads(data)
    assert len(rows) == manifest["counts"]["held_out.json"] >= 300
    assert len({r["id"] for r in rows}) == len(rows)
    assert {r["repo"] for r in rows} == set(manifest["repos"]) == {"verinoda", "orders_app"}
    assert manifest["repos"]["verinoda"]["git_commit"] == "c23c483"
    for rel, sha in manifest["examples"].items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == sha, rel
    # the display hash names exactly the two frozen held-out files
    first = json.loads((ROOT / "benchmarks" / "tq_gold" / "MANIFEST.json").read_text(encoding="utf-8"))
    assert tq_measured.GOLD_FILES == {"tq_gold/held_out.json": first["files"]["held_out.json"],
                                      "tq_gold2/held_out.json": manifest["files"]["held_out.json"]}
    assert tq_measured.gold_on_disk_ok()


def test_every_second_set_case_is_a_well_formed_question():
    rows = json.loads((GOLD2 / "held_out.json").read_text(encoding="utf-8"))
    for r in rows:
        spec = tq.parse_line(r["question"])
        assert spec["type"] in ("exists", "calls")
        assert isinstance(r["answer"], bool)
        assert (r["at"] != []) == r["answer"], r["id"]   # a yes cites its line, a no cites nothing


# -- hashes ---------------------------------------------------------------------------------------------------

def _fake_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    files = {
        "__init__.py": "",
        "tq.py": "from verinoda import naming\n\n\ndef ask():\n    from verinoda.codecheck import api\n"
                 "    return api\n",
        "index.py": "import verinoda.store\n",
        "naming.py": "from . import textnorm\nfrom .sub import deep\n",
        "textnorm.py": "def fold_tr(text):\n    return text.lower()\n",
        "codecheck.py": "def api():\n    pass\n",
        "store.py": "x = 1\n",
        "sub/__init__.py": "",
        "sub/deep.py": "from ..tq_measured import measured\n",
        "tq_measured.py": "def measured():\n    pass\n",
        "benchmark/tq_audit.py": "from verinoda import tq\n",
        "unrelated.py": "y = 2\n",
        "project_index/build.py": "def build():\n    pass\n",
    }
    for rel, text in files.items():
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_bytes(text.encode())
    return pkg


def test_the_engine_hash_covers_the_import_closure_of_tq_and_ignores_line_endings(tmp_path):
    pkg = _fake_pkg(tmp_path)
    assert tq_measured.engine_files(pkg) == [
        "__init__.py", "codecheck.py", "index.py", "naming.py", "project_index/build.py", "store.py",
        "sub/__init__.py", "sub/deep.py", "textnorm.py", "tq.py"]
    h = tq_measured.engine_sha(pkg)
    assert h and len(h) == 64
    for rel in ("unrelated.py", "tq_measured.py", "benchmark/tq_audit.py"):   # outside the engine
        (pkg / rel).write_bytes(b"z = 3\n")
        assert tq_measured.engine_sha(pkg) == h, rel
    (pkg / "tq.py").write_bytes((pkg / "tq.py").read_bytes().replace(b"\n", b"\r\n"))   # CRLF checkout
    assert tq_measured.engine_sha(pkg) == h
    # a module reached only through another module, and one imported inside a function
    for rel in ("textnorm.py", "codecheck.py", "sub/deep.py"):
        before = tq_measured.engine_sha(pkg)
        (pkg / rel).write_bytes((pkg / rel).read_bytes() + b"# edited\n")
        assert tq_measured.engine_sha(pkg) != before, rel
    # a new import pulls a module in; a module created where an import pointed is picked up
    (pkg / "naming.py").write_bytes(b"from . import textnorm, unrelated\nfrom .sub import deep\n"
                                    b"import verinoda.later\n")
    assert "unrelated.py" in tq_measured.engine_files(pkg) and "later.py" not in tq_measured.engine_files(pkg)
    (pkg / "later.py").write_bytes(b"w = 1\n")
    assert "later.py" in tq_measured.engine_files(pkg)
    (pkg / "project_index" / "extra.py").write_bytes(b"v = 1\n")
    assert "project_index/extra.py" in tq_measured.engine_files(pkg)
    (pkg / "project_index" / "build.py").unlink()
    assert tq_measured.engine_sha(pkg)
    (pkg / "tq.py").unlink()
    assert tq_measured.engine_sha(pkg) is None


def test_the_engine_hash_of_the_package_reaches_what_naming_and_the_scope_lib_path_use():
    files = tq_measured.engine_files()
    for rel in ("tq.py", "index.py", "naming.py", "textnorm.py", "entail.py", "evidence.py", "codecheck.py",
                "search_index.py", "graphquery.py"):
        assert rel in files, rel
    assert "tq_measured.py" not in files and not any(f.startswith("benchmark/") for f in files)


def test_an_edit_to_a_module_naming_imports_hides_measured(tmp_path):
    # the committed table is current for this checkout, so its cells show until textnorm.py changes
    pkg = tmp_path / "verinoda"
    shutil.copytree(tq_measured.PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    assert tq_measured.shown_cells(pkg=pkg)
    p = pkg / "textnorm.py"
    p.write_bytes(p.read_bytes() + b"\n# edited\n")
    assert tq_measured.shown_cells(pkg=pkg) == {}


def test_the_gold_hash_is_order_free_and_changes_with_any_file():
    files = dict(tq_measured.GOLD_FILES)
    assert tq_measured.gold_sha(dict(reversed(list(files.items())))) == tq_measured.GOLD_SHA
    files["tq_gold2/held_out.json"] = "0" * 64
    assert tq_measured.gold_sha(files) != tq_measured.GOLD_SHA


def test_wilson_interval():
    assert tq_measured.wilson(0, 0) == (0.0, 1.0)
    lo, hi = tq_measured.wilson(30, 30)
    assert round(lo, 4) == 0.8865 and hi == 1.0
    lo, hi = tq_measured.wilson(41, 42)
    assert 0.87 < lo < 0.88 and 0.99 < hi < 1.0


# -- the display rule -------------------------------------------------------------------------------------------

def _table(tmp_path: Path, pkg: Path, **over) -> Path:
    cells = [{"type": "calls", "answer": "yes", "status": "statically_verified", "n": 40, "right": 39,
              "shown": True, "options": ["", "depth=3"]},
             {"type": "calls", "answer": "no", "status": "weak_inference", "n": 12, "right": 9, "shown": True,
              "options": [""]},
             {"type": "exists", "answer": "no", "status": "strong_inference", "n": 50, "right": 50,
              "shown": False, "options": [""]},
             {"type": "exists", "answer": "yes", "status": "strong_inference", "n": 50, "right": 50,
              "shown": True},   # no options recorded: never shown
             {"type": "exists", "answer": "?", "status": "unknown", "n": 31, "right": 0, "shown": True,
              "options": [""]}]
    data = {"schema": tq_measured.SCHEMA, "gold_files": dict(tq_measured.GOLD_FILES),
            "gold_sha": tq_measured.GOLD_SHA, "engine": {"sha": tq_measured.engine_sha(pkg)},
            "score_sha": tq_measured.scorer_sha(), "cells": cells}
    data.update(over)
    p = tmp_path / "table.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_only_cells_with_30_decided_answers_marked_shown_are_shown(tmp_path):
    pkg = _fake_pkg(tmp_path)
    cells = tq_measured.shown_cells(_table(tmp_path, pkg), pkg=pkg)
    sha8 = tq_measured.GOLD_SHA[:8]
    assert cells == {"calls|yes|statically_verified|": f"39/40 held-out @{sha8}",
                     "calls|yes|statically_verified|depth=3": f"39/40 held-out @{sha8}"}
    row = {"answer": True, "status": "statically_verified"}
    assert tq_measured.measured("calls", row, cells) == f"39/40 held-out @{sha8}"
    assert tq_measured.measured("calls", row, cells, "depth=3") == f"39/40 held-out @{sha8}"
    assert tq_measured.measured("calls", row, cells, "depth=2") is None   # no held-out question asked this
    assert tq_measured.measured("calls", {"answer": True, "status": "strong_inference"}, cells) is None
    assert tq_measured.measured("calls", {"answer": None, "status": "unknown"}, cells) is None


def test_a_doctored_engine_hash_or_a_changed_gold_hash_hides_measured(tmp_path):
    pkg = _fake_pkg(tmp_path)
    good = tq_measured.shown_cells(_table(tmp_path, pkg), pkg=pkg)
    assert good
    assert tq_measured.shown_cells(_table(tmp_path, pkg, engine={"sha": "0" * 64}), pkg=pkg) == {}
    assert tq_measured.shown_cells(_table(tmp_path, pkg, gold_sha="1" * 64), pkg=pkg) == {}
    other = dict(tq_measured.GOLD_FILES, **{"tq_gold2/held_out.json": "2" * 64})
    assert tq_measured.shown_cells(_table(tmp_path, pkg, gold_files=other, gold_sha=tq_measured.gold_sha(other)),
                                   pkg=pkg) == {}
    assert tq_measured.shown_cells(_table(tmp_path, pkg, schema="other/1"), pkg=pkg) == {}
    # the scorer that decided right and wrong changed, or the table does not name it
    assert tq_measured.shown_cells(_table(tmp_path, pkg, score_sha="3" * 64), pkg=pkg) == {}
    assert tq_measured.shown_cells(_table(tmp_path, pkg, score_sha=None), pkg=pkg) == {}
    # the engine changed after the table was written
    table = _table(tmp_path, pkg)
    (pkg / "textnorm.py").write_bytes(b"# edited\n")
    assert tq_measured.shown_cells(table, pkg=pkg) == {}
    # a gold file on disk that no longer has its frozen hash
    pkg2 = _fake_pkg(tmp_path / "b")
    table2 = _table(tmp_path / "b", pkg2)
    bench = tmp_path / "bench"
    for rel in tq_measured.GOLD_FILES:
        shutil.copytree(ROOT / "benchmarks" / rel.split("/")[0], bench / rel.split("/")[0], dirs_exist_ok=True)
    assert tq_measured.shown_cells(table2, pkg=pkg2, bench=bench)
    sc = bench / "tq_gold" / "score.py"
    keep = sc.read_bytes()
    sc.write_bytes(keep + b"\n# edited\n")
    assert tq_measured.shown_cells(table2, pkg=pkg2, bench=bench) == {}
    sc.write_bytes(keep)
    p = bench / "tq_gold2" / "held_out.json"
    p.write_bytes(p.read_bytes() + b"\n")
    assert tq_measured.shown_cells(table2, pkg=pkg2, bench=bench) == {}
    p.unlink()
    assert tq_measured.shown_cells(table2, pkg=pkg2, bench=bench) == {}
    # no table, or one that is not JSON
    assert tq_measured.shown_cells(tmp_path / "missing.json", pkg=pkg2) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert tq_measured.shown_cells(bad, pkg=pkg2) == {}


@pytest.mark.skipif(not GIT, reason="git not available")
def test_tq_shows_measured_only_for_a_current_table(tmp_path, monkeypatch):
    repo = _scan({"a.py": "def helper():\n    return 1\n\n\ndef use():\n    return helper()\n"}, tmp_path / "r")
    sha8 = tq_measured.GOLD_SHA[:8]
    cells = {"calls|yes|statically_verified|": f"39/40 held-out @{sha8}",
             "exists|no|strong_inference|": f"82/82 held-out @{sha8}"}
    monkeypatch.setattr(tq_measured, "shown_cells", lambda *a, **k: cells)
    res = tq.ask(repo, ["calls use helper", "calls helper use", "exists nothing_like_it",
                        "calls use helper depth=2", "exists json.no_such_fn_xyz scope=lib"])
    yes, no, absent, deeper, lib = res["answers"]
    assert yes["answer"] is True and yes["status"] == "statically_verified"
    assert yes["measured"] == f"39/40 held-out @{sha8}"
    assert absent["answer"] is False and absent["measured"] == f"82/82 held-out @{sha8}"
    assert "measured" not in no
    # the same cells asked with options no held-out question used show nothing: a deeper call, and an
    # absence decided by reading the installed library instead of the index
    assert deeper["answer"] is True and deeper["status"] == "statically_verified" and "measured" not in deeper
    assert "measured" not in lib
    # the table was measured with verify on; without it nothing is shown, not even an absence
    assert "measured" not in tq.ask(repo, ["exists nothing_like_it"], verify=False)["answers"][0]
    assert f"measured: 39/40 held-out @{sha8}" in tq.render(res)
    assert list(yes) == [k for k in tq._KEY_ORDER if k in yes]
    monkeypatch.setattr(tq_measured, "shown_cells", lambda *a, **k: {})
    assert "measured" not in tq.ask(repo, ["calls use helper"])["answers"][0]

    def broken(*a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(tq_measured, "shown_cells", broken)
    assert tq.ask(repo, ["calls use helper"])["answers"][0]["answer"] is True


# -- the audit ----------------------------------------------------------------------------------------------------

def _row(t, kind, status, verdict, split="held_out"):
    return {"type": t, "answer_kind": kind, "status": status, "verdict": verdict, "split": split}


def test_the_calibration_shows_a_cell_only_with_30_answers_and_a_consistent_dev_cell():
    held = ([_row("calls", "yes", "statically_verified", "right")] * 30
            + [_row("exists", "no", "strong_inference", "right")] * 29
            + [_row("callers", "count", "strong_inference", "right")] * 40
            + [_row("calls", "no", "weak_inference", "right")] * 20 + [_row("calls", "no", "weak_inference", "wrong")] * 15
            + [_row("reaches", "?", "unknown", "unknown")] * 35)
    dev = ([_row("calls", "yes", "statically_verified", "right", "dev")] * 5
           + [_row("calls", "no", "weak_inference", "right", "dev")] * 3
           + [_row("exists", "no", "strong_inference", "right", "dev")] * 2)
    cells = {(c["type"], c["answer"], c["status"]): c for c in tq_audit.calibration(held, dev)}
    yes = cells[("calls", "yes", "statically_verified")]
    assert yes["shown"] and yes["n"] == 30 and yes["right"] == 30 and yes["dev_inside"] is True
    assert not cells[("exists", "no", "strong_inference")]["shown"]          # n = 29
    assert cells[("callers", "count", "strong_inference")]["why_not_shown"] == "no dev answer in this cell"
    weak = cells[("calls", "no", "weak_inference")]   # 20/35 held-out, dev 3/3 lies above the interval
    assert not weak["shown"] and weak["dev_inside"] is False
    assert cells[("reaches", "?", "unknown")]["why_not_shown"] == "not a decided answer"


def test_the_reliability_table_puts_each_status_next_to_its_cap():
    from verinoda.claims import CONFIDENCE_CAP

    held = ([_row("calls", "yes", "statically_verified", "right")] * 9
            + [_row("calls", "yes", "statically_verified", "wrong")]
            + [_row("calls", "no", "weak_inference", "right")] * 4 + [_row("x", "?", "unknown", "unknown")] * 3)
    rel = {r["status"]: r for r in tq_audit.reliability(held)}
    assert set(CONFIDENCE_CAP) <= set(rel)
    sv = rel["statically_verified"]
    assert (sv["n"], sv["right"], sv["cap"]) == (10, 9, CONFIDENCE_CAP["statically_verified"])
    assert sv["cap_vs_interval"] == "inside"
    assert rel["weak_inference"]["cap_vs_interval"] == "below"   # 4/4 right; cap 0.4 under the interval
    assert rel["unknown"]["n"] == 0 and rel["observed"]["n"] == 0


def test_a_gold_file_that_changed_stops_the_audit(tmp_path):
    bench = tmp_path / "bench"
    shutil.copytree(ROOT / "benchmarks" / "tq_gold2", bench / "tq_gold2")
    p = bench / "tq_gold2" / "held_out.json"
    p.write_bytes(p.read_bytes().replace(b"exists", b"exist", 1))
    with pytest.raises(tq_audit.GoldError):
        tq_audit.load_gold("tq_gold2/held_out.json", bench)
    with pytest.raises(tq_audit.GoldError):
        tq_audit.load_gold("tq_gold2/missing.json", bench)


@pytest.mark.skipif(not GIT, reason="git not available")
def test_the_audit_runs_tq_on_a_frozen_set_and_writes_the_table_and_report(tmp_path):
    src = tmp_path / "src"
    (src / "m.py").parent.mkdir(parents=True)
    (src / "m.py").write_text("def helper():\n    return 1\n\n\ndef use():\n    return helper()\n\n\n"
                              "def lone():\n    return 2\n", encoding="utf-8", newline="\n")
    bench = tmp_path / "bench"
    shutil.copytree(ROOT / "benchmarks" / "tq_gold", bench / "tq_gold",
                    ignore=shutil.ignore_patterns("fixtures", "__pycache__"))
    cases = [{"id": "x1", "repo": "m", "question": "calls use helper", "answer": True, "at": ["m.py:6"]},
             {"id": "x2", "repo": "m", "question": "calls lone helper", "answer": False, "at": []},
             {"id": "x3", "repo": "m", "question": "exists helper", "answer": True, "at": ["m.py:1"]},
             {"id": "x4", "repo": "m", "question": "exists nothing_here", "answer": True, "at": []}]
    text = json.dumps(cases, indent=1) + "\n"
    (bench / "tqx").mkdir()
    (bench / "tqx" / "held_out.json").write_text(text, encoding="utf-8", newline="\n")
    (bench / "tqx" / "MANIFEST.json").write_text(json.dumps(
        {"files": {"held_out.json": hashlib.sha256(text.encode()).hexdigest()},
         "repos": {"m": {"source": str(src)}}}), encoding="utf-8")
    res = tq_audit.evaluate(work=tmp_path / "work", bench=bench, held_out=("tqx/held_out.json",), dev=None)
    rows = {r["id"]: r for r in res["report"]["rows"]}
    assert rows["x1"]["verdict"] == "right" and rows["x1"]["status"] == "statically_verified"
    assert rows["x2"]["verdict"] == "right" and rows["x2"]["answer_kind"] == "no"
    assert rows["x4"]["verdict"] == "wrong" and rows["x4"]["status"] == "strong_inference"
    s = res["report"]["summary"]
    assert (s["held_out_cases"], s["decided"], s["wrong"], s["cells_shown"]) == (4, 4, 1, 0)
    assert res["table"]["gold_files"] == {"tqx/held_out.json": hashlib.sha256(text.encode()).hexdigest()}
    assert res["table"]["engine"]["sha"] == tq_measured.engine_sha()
    assert [w["id"] for w in res["report"]["wrong"]] == ["x4"]
    out = tq_audit.write(res, table=tmp_path / "t.json", report_dir=tmp_path / "rep")
    assert json.loads(Path(out["table"]).read_text(encoding="utf-8"))["cells"]
    md = Path(out["report_md"]).read_text(encoding="utf-8")
    assert "CONFIDENCE_CAP" in md and "`x4`" in md
    # a table keyed by another gold set is never shown
    assert tq_measured.shown_cells(tmp_path / "t.json") == {}


def test_the_cli_runs_the_audit_and_says_where_it_wrote(monkeypatch, capsys):
    fake = {"table": {"gold_sha": "g" * 64, "engine": {"sha": "e" * 64}},
            "report": {"summary": {"wrong_at_verified": [], "cells_shown": 2},
                       "reliability": [{"status": "strong_inference", "right": 9, "n": 10, "cap": 0.7}]}}
    monkeypatch.setattr(tq_audit, "evaluate", lambda **k: fake)
    assert cli.main(["benchmark", "tq-audit", "--no-write", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reliability"] == {"strong_inference": "9/10 (cap 0.7)"} and out["written"] == {}
    fake["report"]["summary"]["wrong_at_verified"] = ["x"]
    assert cli.main(["benchmark", "tq-audit", "--no-write", "--json"]) == 1

    def gold_error(**k):
        raise tq_audit.GoldError("changed")

    monkeypatch.setattr(tq_audit, "evaluate", gold_error)
    assert cli.main(["benchmark", "tq-audit", "--no-write"]) == 2


# -- the committed table --------------------------------------------------------------------------------------------

def test_the_committed_table_is_keyed_by_the_frozen_gold_and_follows_the_display_rule():
    data = json.loads(tq_measured.TABLE.read_text(encoding="utf-8"))
    assert data["schema"] == tq_measured.SCHEMA
    assert data["gold_files"] == tq_measured.GOLD_FILES and data["gold_sha"] == tq_measured.GOLD_SHA
    assert len(data["engine"]["sha"]) == 64 and data["min_n"] == tq_measured.MIN_N
    for c in data["cells"]:
        lo, hi = tq_measured.wilson(c["right"], c["n"])
        assert (round(lo, 4), round(hi, 4)) == (c["wilson_lo"], c["wilson_hi"])
        if c["shown"]:
            assert c["n"] >= 30 and c["answer"] not in ("?", "invalid") and c["dev_n"] > 0
            assert lo <= c["dev_right"] / c["dev_n"] <= hi
    report = sorted((ROOT / "benchmarks" / "results").glob("tq-audit-*/report.json"))[-1]
    rep = json.loads(report.read_text(encoding="utf-8"))
    assert rep["gold_sha"] == data["gold_sha"] and rep["engine_sha"] == data["engine"]["sha"]
    assert {r["status"] for r in rep["reliability"]} >= {"statically_verified", "strong_inference", "weak_inference"}
