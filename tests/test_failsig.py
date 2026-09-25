"""Failure signatures: the pytest plugin, the log parsers, symbol mapping and the two keys."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as hst  # noqa: E402

from verinoda import experiments, failsig  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "failsig"
EXAMPLE = ROOT / "examples" / "orders_app"


# The fixture sources carry a `.fixture` suffix (and the logs `.log`) so that indexing this repository does
# not take them for its own code or documents; the tests see them under their real names.
SUFFIX = ".fixture"


def _project(name: str) -> Path:
    return EXAMPLE if name == "orders_app" else FIX / "projects" / name


def _files(root: Path) -> list[str]:
    out = []
    for p in root.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            rel = p.relative_to(root).as_posix()
            if FIX not in root.parents or rel.endswith(SUFFIX):
                out.append(rel[:-len(SUFFIX)] if FIX in root.parents else rel)
    return out


def _reader(root: Path):
    def read(rel: str):
        try:
            return (root / (rel + SUFFIX) if FIX in root.parents else root / rel).read_bytes()
        except OSError:
            return None
    return read


CASES = json.loads((FIX / "cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["case"])
def test_log_fixtures_give_the_gold_exception_and_symbol(case):
    root = _project(case["project"])
    log = (FIX / "logs" / f"{case['case']}.log").read_text(encoding="utf-8")
    sig = failsig.extract(log, "", outcome="fail", files=_files(root), reader=_reader(root))
    got = sorted({(f["exc"], f["at"]) for f in sig["failures"]})
    assert got == sorted(tuple(g) for g in case["gold"]), sig
    assert sig["status"] == "parsed"
    exact, coarse = failsig.keys(sig)
    assert exact and coarse


def test_a_passing_run_has_no_signature():
    assert failsig.extract("5 passed in 0.1s", "", outcome="pass", files=[])["status"] == "none"


def test_unparsable_output_is_unknown_and_has_no_keys():
    sig = failsig.extract("Segmentation fault (core dumped)\n", "", outcome="fail", files=["a.py"])
    assert sig["status"] == "unknown" and failsig.keys(sig) == (None, None)
    partial = failsig.extract("FAILED tests/test_x.py::test_y\n", "", outcome="fail", files=["tests/test_x.py"])
    assert partial["status"] == "partial" and failsig.keys(partial) == (None, None)


def _tb(line: int, msg: str = "'quantity'") -> str:
    return ("Traceback (most recent call last):\n"
            f'  File "/tmp/x/verinoda-exp-ab12/repo/orders/pricing.py", line {line}, in compute_total\n'
            "    subtotal = 1\n"
            f"KeyError: {msg}\n")


def test_a_line_shift_keeps_both_keys_and_a_new_message_only_the_coarse_one():
    files = ["orders/pricing.py"]
    root = EXAMPLE
    a = failsig.extract(_tb(7), "", outcome="fail", files=files, reader=_reader(root))
    b = failsig.extract(_tb(6), "", outcome="fail", files=files, reader=_reader(root))  # still inside compute_total
    c = failsig.extract(_tb(7, "'price'"), "", outcome="fail", files=files, reader=_reader(root))
    assert a["failures"][0]["at"] == "orders/pricing.py::compute_total"
    assert failsig.keys(a) == failsig.keys(b)
    assert failsig.keys(a)[0] != failsig.keys(c)[0] and failsig.keys(a)[1] == failsig.keys(c)[1]


def test_messages_are_normalised_for_addresses_ids_and_copy_paths():
    m1 = failsig.norm_msg(r"bad <Obj at 0x7f00aa> in C:\T\verinoda-exp-1a2b\repo\orders\x.py id 12345678")
    m2 = failsig.norm_msg(r"bad <Obj at 0x1234ff> in /tmp/verinoda-exp-zz/repo/orders\x.py id 99999999")
    assert m1 == m2 == r"bad <Obj at 0x?> in orders\x.py id <n>"


def test_messages_are_normalised_for_repr_paths_times_hex_ids_and_ports():
    # review finding: these made a deterministic failure look different on every run ("flaky")
    tmp1 = ("[Errno 2] No such file or directory: 'C:\\\\Users\\\\u\\\\AppData\\\\Local\\\\Temp\\\\verinoda-exp-9dvnogvv"
            "\\\\_home\\\\pytest-of-unknown\\\\pytest-0\\\\test_export0\\\\out\\\\orders.csv'")
    tmp2 = tmp1.replace("9dvnogvv", "jvxpxqjd")
    assert failsig.norm_msg(tmp1) == failsig.norm_msg(tmp2) and "verinoda-exp" not in failsig.norm_msg(tmp1)
    mk1 = "cannot write 'C:\\\\T\\\\verinoda-exp-ab12\\\\_home\\\\tmpk3j4x\\\\out.csv'"
    assert failsig.norm_msg(mk1) == failsig.norm_msg(mk1.replace("ab12", "zz99").replace("k3j4x", "q8w7e"))
    pairs = [("item without qty at 2026-09-25T15:18:29.776745", "item without qty at 2026-09-25T15:19:02.001"),
             ("request e3b0c44298fc1c149afbf4c8996fb924 rejected", "request 9f86d081884c7d659a2feaa0c55ad015 rejected"),
             ("token 9f86d081884c7d65", "token 1b4f0e9851971998"),
             ("connect to 127.0.0.1:54321 refused", "connect to 127.0.0.1:61234 refused"),
             ("pid 18234 exited", "pid 2231 exited"), ("at 10:01:02.5", "at 11:59:59.25")]
    for a, b in pairs:
        assert failsig.norm_msg(a) == failsig.norm_msg(b), (a, failsig.norm_msg(a), failsig.norm_msg(b))
    # what a failure is about stays: numbers in assertions, plain words
    assert failsig.norm_msg("assert 45.0 == 50.0") != failsig.norm_msg("assert 18.0 == 20.0")
    assert failsig.norm_msg("KeyError: 'deadbeef'") == "KeyError: 'deadbeef'"


def test_path_resolution_needs_a_whole_suffix():
    r = failsig.PathResolver(["orders/__init__.py", "orders/pricing.py", "pricing.go", "src/test/java/a/B.java"],
                             roots=["C:/Users/me/proj"])
    assert r.resolve(r"C:\Python312\Lib\importlib\__init__.py") is None
    assert r.resolve(r"C:\Users\me\proj\orders\pricing.py") == "orders/pricing.py"
    assert r.resolve("/tmp/verinoda-exp-1/repo/orders/pricing.py") == "orders/pricing.py"
    assert r.resolve("/home/dev/orders/pricing.go") == "pricing.go"
    assert r.resolve("B.java") == "src/test/java/a/B.java"
    assert r.resolve("/usr/lib/python3.12/site-packages/orders/pricing.py") is None


def test_python_syntax_errors_map_by_indentation():
    src = b"class A:\n    def f(self):\n        x = (\n\n\ndef g():\n    pass\n"
    m = failsig.SymbolMapper(lambda rel: src)
    assert m.symbol("m.py", 3) == "A.f"
    assert m.symbol("m.py", 7) == "g"


@settings(max_examples=150, deadline=None)
@given(hst.text(alphabet=hst.characters(blacklist_categories=("Cs",)), max_size=400))
def test_parsers_never_raise_on_arbitrary_text(text):
    probe = ("Traceback (most recent call last):\n" if len(text) % 2 else "--- FAIL: T\n") + text \
        + "\nthread 'x' panicked at src/lib.rs:1:1:\n    at a.b.C.m(C.java:3)\n"
    sig = failsig.extract(probe, text, outcome="fail", files=["src/lib.rs", "a/b/C.java"], reader=lambda r: None)
    assert sig["status"] in ("parsed", "partial", "unknown")


# -- the pytest plugin, through a real isolated run --------------------------------------------------

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.mark.experiment
@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_the_plugin_reports_exception_frames_and_outcomes(tmp_path):
    repo = tmp_path / "oa"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8").replace('i["qty"]', 'i["quantity"]'), encoding="utf-8")
    ids: dict[str, str] = {}
    res = experiments.run(open_store(repo), repo,
                          ["python", "-m", "pytest", "-q", "-p", failsig.PLUGIN_MODULE, "-p", "no:cacheprovider"],
                          hypothesis="plugin", plugins={f"{failsig.PLUGIN_MODULE}.py": failsig.plugin_source()},
                          file_ids=ids)
    assert res["outcome"] == "fail"
    data = Path(res["artifacts"][failsig.PLUGIN_FILE]).read_bytes()
    sig = failsig.extract("", "", outcome="fail", files=ids, reader=_reader(repo), plugin_data=data)
    assert sig["parser"] == "pytest_plugin" and sig["status"] == "parsed"
    assert {(f["exc"], f["at"]) for f in sig["failures"]} == {("KeyError", "orders/pricing.py::compute_total")}
    assert sig["failed_tests"] == ["tests/test_pricing.py::test_compute_total",
                                   "tests/test_service.py::test_place_and_fetch_roundtrip"]
    assert sig["tests"]["tests/test_pricing.py::test_no_discount_below_threshold"] == "passed"
    f = next(f for f in sig["failures"] if f["test"].endswith("roundtrip"))
    assert [x["symbol"] for x in f["frames"]] == ["test_place_and_fetch_roundtrip", "place_order", "compute_total",
                                                  "compute_total"]
    ok = experiments.run(open_store(repo), repo, ["python", "-m", "pytest", "-q", "-p", failsig.PLUGIN_MODULE,
                                                  "tests/test_service.py::test_empty_order_rejected"],
                         hypothesis="plugin on a pass", plugins={f"{failsig.PLUGIN_MODULE}.py": failsig.plugin_source()})
    assert ok["outcome"] == "pass"


def _plugin_run(repo: Path, *args: str) -> tuple[dict, dict]:
    ids: dict[str, str] = {}
    res = experiments.run(open_store(repo), repo, ["python", "-m", "pytest", "-q", "-p", failsig.PLUGIN_MODULE,
                                                   "-p", "no:cacheprovider", *args], hypothesis="plugin",
                          plugins={f"{failsig.PLUGIN_MODULE}.py": failsig.plugin_source()}, file_ids=ids)
    data = Path(res["artifacts"][failsig.PLUGIN_FILE]).read_bytes()
    out = Path(res["logs"]["stdout"]).read_text(encoding="utf-8")
    sig = failsig.extract(out, "", outcome="pass" if res["outcome"] == "pass" else "fail", files=ids,
                          reader=_reader(repo), plugin_data=data)
    return res, sig


@pytest.mark.experiment
@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_collection_errors_doctests_early_stops_and_path_ids(tmp_path):
    # review findings: a collection error and a doctest failure had no location ('? at ?'); -x left later tests
    # "not run" as if deselected; a test parametrised over absolute paths got a new id in every copy
    repo = tmp_path / "oa"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.db"))
    (repo / "tests" / "cases").mkdir()
    (repo / "tests" / "cases" / "big.json").write_text('{"total": 1}', encoding="utf-8")
    (repo / "tests" / "test_cases.py").write_text(
        "import glob, os\nimport pytest\n\nHERE = os.path.dirname(os.path.abspath(__file__))\n\n\n"
        "@pytest.mark.parametrize('path', sorted(glob.glob(os.path.join(HERE, 'cases', '*.json'))))\n"
        "def test_case(path):\n    assert path.endswith('small.json')\n", encoding="utf-8")
    p = repo / "orders" / "pricing.py"
    text = p.read_text(encoding="utf-8")
    assert "def apply_discount(subtotal: float) -> float:\n" in text
    p.write_text(text.replace("def apply_discount(subtotal: float) -> float:\n",
                              "def apply_discount(subtotal: float) -> float:\n"
                              "    \"\"\"\n    >>> apply_discount(200.0)\n    170.0\n    \"\"\"\n"),
                 encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    res, sig = _plugin_run(repo, "--doctest-modules", "orders")
    assert res["outcome"] == "fail" and sig["status"] == "parsed"
    f = sig["failures"][0]
    assert f["exc"] == "DocTestFailure" and f["at"] == "orders/pricing.py::apply_discount"
    _, a = _plugin_run(repo, "tests/test_cases.py")
    _, b = _plugin_run(repo, "tests/test_cases.py")
    assert a["failed_tests"] == b["failed_tests"] and len(a["failed_tests"]) == 1
    assert "verinoda-exp" not in json.dumps(a["failed_tests"] + list(a["tests"]))
    assert failsig.keys(a) == failsig.keys(b)
    _, x = _plugin_run(repo, "-x", "tests/test_cases.py", "tests/test_pricing.py")
    assert x["stopped_early"] is True
    p.write_text(text.replace("def compute_total(", "def compute_order_total("), encoding="utf-8")
    res, sig = _plugin_run(repo, "tests/test_pricing.py")
    assert res["outcome"] == "inconclusive" and sig["status"] == "parsed"  # pytest exit 2: a collection error
    f = sig["failures"][0]
    assert f["exc"] == "ImportError" and f["phase"] == "collect" and f["test"] == "tests/test_pricing.py"
    assert sig["failed_tests"] == ["tests/test_pricing.py"]


def test_pytest_summary_counts():
    assert failsig.pytest_counts("..ss. [100%]\n3 passed, 2 skipped in 0.21s\n") == {"passed": 3, "skipped": 2}
    assert failsig.pytest_counts("=== 1 failed, 1 error, 4 deselected in 1.02s ===") == {
        "failed": 1, "error": 1, "deselected": 4}
    assert failsig.pytest_counts("no tests ran in 0.01s") == {}
    assert failsig.pytest_counts("BUILD SUCCESSFUL in 3s") is None


def test_test_ids_lose_the_runs_throw_away_paths():
    raw = ("tests/test_cases.py::test_case[C:\\\\Users\\\\u\\\\AppData\\\\Local\\\\Temp\\\\verinoda-exp-b84geob5\\\\repo"
           "\\\\tests\\\\cases\\\\big.json]")
    assert failsig.norm_test_id(raw) == "tests/test_cases.py::test_case[tests\\\\cases\\\\big.json]"
    assert failsig.norm_test_id("tests/t.py::test_x[/tmp/verinoda-exp-1a2b/repo/tests/c/x.json]") == \
        "tests/t.py::test_x[tests/c/x.json]"
    assert failsig.norm_test_id("tests/t.py::test_x[1-2]") == "tests/t.py::test_x[1-2]"
