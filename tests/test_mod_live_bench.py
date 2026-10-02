"""The edit-to-fresh benchmark of the ``verinoda-live`` mod (benchmarks/mod_live/run.py): its edits, its
freshness checks, its summary and its path sanitizing; one end-to-end run is marked ``slow``."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("mod_live_run", ROOT / "benchmarks" / "mod_live" / "run.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["mod_live_run"] = bench  # dataclasses look their module up by name
_spec.loader.exec_module(bench)


@pytest.fixture
def twins(tmp_path):
    out = []
    for name in ("a", "b"):
        dest = tmp_path / name
        shutil.copytree(bench.DEFAULT_CORPUS, dest, ignore=bench.IGNORE)
        out.append(dest)
    return out


def _tree(repo: Path) -> dict[str, bytes]:
    return {p.relative_to(repo).as_posix(): p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}


def test_python_files_leave_out_tests_and_the_benchmarks_own_modules(twins):
    repo = twins[0]
    (repo / "bench_mod_0.py").write_text("x = 1\n", encoding="utf-8")
    files = bench.python_files(repo)
    assert files and all("tests/" not in f and not f.startswith("bench_mod_") for f in files)
    assert files == sorted(files)


@pytest.mark.parametrize("steps", [["comment"], ["body"], ["batch:3"], ["add", "del"]])
def test_both_twins_get_the_same_bytes(twins, steps):
    a, b = twins
    for step in steps:
        assert bench.apply_edit(a, step, 0) == bench.apply_edit(b, step, 0)
    assert _tree(a) == _tree(b)


def test_edits_introduce_and_remove_the_names_they_report(twins):
    repo = twins[0]
    body = bench.apply_edit(repo, "body", 1)
    assert f"def {body.symbol}(" in (repo / body.files[0]).read_text(encoding="utf-8")
    batch = bench.apply_edit(repo, "batch:3", 1)
    assert len(batch.files) == 3 and batch.symbol == "modlive_batch_3_r1_0"
    added = bench.apply_edit(repo, "add", 1)
    assert (repo / added.files[0]).is_file()
    deleted = bench.apply_edit(repo, "del", 1)
    assert not (repo / deleted.files[0]).exists() and deleted.gone == added.symbol and deleted.symbol is None
    assert bench.apply_edit(repo, "comment", 1).symbol is None


def test_del_needs_an_add_and_unknown_steps_are_refused(twins):
    with pytest.raises(ValueError, match="earlier 'add'"):
        bench.apply_edit(twins[0], "del", 0)
    with pytest.raises(ValueError, match="unknown step"):
        bench.apply_edit(twins[0], "rename", 0)


def test_a_hit_is_an_item_never_the_echoed_question():
    echoed = {"question": "symbol:gone_fn", "filters": {"expression": "symbol:gone_fn"}, "items": []}
    assert bench.hit_symbols(echoed) == set()
    hit = {"question": "symbol:compute_total", "items": [{"symbol": "compute_total()"}, {"symbol": "Cart"}]}
    assert bench.hit_symbols(hit) == {"compute_total", "Cart"}
    assert bench.hit_symbols({"items": "not a list"}) == set()


def test_summary_counts_rates_errors_and_speedup():
    M = bench.Measure
    rows = [
        bench.StepResult(0, "body", ["a.py"], [M("fast", 1.0, 0, True, False, graph_pending=True, graph_seconds=3.0,
                                                  graph_fresh=True, leftover_changes=0),
                                                M("full", 4.0, 0, True, True, graph_fresh=True, leftover_changes=0)]),
        bench.StepResult(1, "body", ["a.py"], [M("fast", 3.0, 1, False, False, graph_seconds=5.0, graph_fresh=False,
                                                  leftover_changes=2),
                                                M("full", 2.0, 0, True, True, graph_fresh=True, leftover_changes=0)]),
    ]
    s = bench.summarize(rows)["body"]
    assert s["fast"]["median_seconds"] == 2.0 and s["full"]["median_seconds"] == 3.0 and s["speedup"] == 1.5
    assert s["fast"]["median_graph_seconds"] == 4.0 and s["full"]["median_graph_seconds"] is None
    assert s["fast"]["errors"] == 1 and s["fast"]["leftover_changes"] == 2
    assert s["fast"]["text_now"] == "1/2" and s["fast"]["symbol_now"] == "0/2"
    assert s["fast"]["graph_pending"] == "1/2" and s["full"]["graph_fresh"] == "2/2"
    assert s["fast"]["gone_absent"] is None


def test_sanitize_takes_the_most_specific_path_first_in_both_slash_styles():
    reps = {"C:\\work\\repo\\examples\\app": "<CORPUS>", "C:\\work\\repo": "<REPO>"}
    value = {"a": ["C:/work/repo/examples/app/x.py", "C:\\work\\repo\\README.md"], "n": 3}
    assert bench.sanitize(value, reps) == {"a": ["<CORPUS>/x.py", "<REPO>\\README.md"], "n": 3}


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_end_to_end_both_modes_end_fresh(tmp_path):
    body = bench.run_benchmark(sys.executable, bench.DEFAULT_CORPUS, tmp_path, ["body", "add", "del"], 1,
                               log=lambda _: None)
    for step, by_mode in body["summary"].items():
        for mode in ("fast", "full"):
            m = by_mode[mode]
            assert m["errors"] == 0 and m["leftover_changes"] == 0, (step, mode, m)
            assert m["graph_fresh"] in (None, "1/1") and m["gone_absent"] in (None, "1/1"), (step, mode, m)
        assert by_mode["full"]["text_now"] in (None, "1/1") and by_mode["full"]["symbol_now"] in (None, "1/1")
        assert by_mode["fast"]["median_graph_seconds"] is not None
