"""verinoda probe (docs/DESIGN.md D36): inputs, the side-effect gate, the plugin, the oracles, and real runs.

The unit tests read syntax trees only. The tests marked ``experiment`` run the probe end to end on git copies of
examples/orders_app: real isolated runs through verinoda.experiments (a few seconds each).
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import ast  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, entail, probe, probe_gate  # noqa: E402
from verinoda import probe_inputs as pin  # noqa: E402
from verinoda.runtime import probe_plugin as plug  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
AD_OLD = "if subtotal > DISCOUNT_THRESHOLD:\n        return round(subtotal * 0.9, 2)"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, extra: dict[str, str] | None = None, name: str = "oa") -> Path:
    dst = tmp_path / name
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    for rel, text in (extra or {}).items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _plain_repo(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    """A copy without git (for the static parts)."""
    dst = tmp_path / "plain"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    for rel, text in (extra or {}).items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    return dst


def _sub(repo: Path, rel: str, old: str, new: str) -> None:
    p = repo / rel
    t = p.read_text(encoding="utf-8")
    assert old in t
    p.write_bytes(t.replace(old, new, 1).encode("utf-8"))


def _files(repo: Path) -> list[str]:
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()
                  and ".git" not in p.relative_to(repo).parts and ".verinoda" not in p.relative_to(repo).parts)


def _digest(repo: Path) -> dict[str, str]:
    return {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(repo.rglob("*")) if p.is_file() and ".verinoda" not in p.relative_to(repo).parts
            and ".git" not in p.relative_to(repo).parts}


# -- inputs (syntax trees only) ------------------------------------------------------------------------

def _ann(src: str, resolve=None) -> dict:
    return pin.annotation_td(ast.parse(src, mode="eval").body, resolve)


def test_annotations_become_type_descriptors():
    assert _ann("int") == {"k": "int"} and _ann("float") == {"k": "float"} and _ann("'str'") == {"k": "str"}
    assert _ann("Optional[int]") == {"k": "optional", "of": {"k": "int"}}
    assert _ann("int | None") == {"k": "optional", "of": {"k": "int"}}
    assert _ann("list[dict]")["k"] == "list" and _ann("typing.List[float]") == {"k": "list", "of": {"k": "float"}}
    assert _ann("tuple[int, ...]") == {"k": "tuple", "of": {"k": "int"}}
    assert _ann("tuple[int, str]") == {"k": "tuple", "items": [{"k": "int"}, {"k": "str"}]}
    assert _ann("dict[str, int]") == {"k": "dict", "key": {"k": "str"}, "val": {"k": "int"}}
    assert _ann("Literal['a', 'b']") == {"k": "literal", "values": ["a", "b"]}
    assert _ann("Union[int, str]")["k"] == "union"
    assert _ann("Callable[[int], int]")["k"] == "unsupported"
    assert _ann("Widget")["k"] == "unsupported" and "Widget" in _ann("Widget")["why"]
    assert _ann("Widget", lambda n: {"k": "recipe", "recipes": [{"$new": {"m": "w", "n": n}}]})["k"] == "recipe"
    assert pin.annotation_td(None)["k"] == "any"


def test_call_site_literals_refine_an_annotation_into_a_shape():
    obs = pin.merge_observed([pin.observed_td([{"price": 10.0, "qty": 2}])])
    td = pin.refine(_ann("list[dict]"), obs)
    assert td["k"] == "list" and td["of"]["k"] == "shape"
    assert td["of"]["keys"] == {"price": {"k": "float"}, "qty": {"k": "int"}}
    # the unit variant makes a boundary reachable through price * qty
    assert pin._unit_shape(td["of"]) == {"price": 1.0, "qty": 1}
    assert pin.in_domain(td) and not pin.in_domain({"k": "any"})


def test_encoding_round_trips_and_renders_as_source():
    values = [None, True, 3, -0.0, 1e308, float("inf"), "İstanbul", b"\x00\xff", (1, "a"), {"k": [1, 2]},
              frozenset({1}), {2}]
    for v in values:
        enc = pin.encode(v)
        json.dumps(enc)  # JSON-safe
        back = plug.decode(json.loads(json.dumps(enc)))
        assert back == v or (isinstance(v, float) and math.isnan(v) and math.isnan(back))
        assert pin.plain(enc) == back
    nan = plug.decode(pin.encode(float("nan")))
    assert math.isnan(nan)
    assert pin.to_source(pin.encode(float("-inf"))) == "float('-inf')"
    seq = {"$seq": [{"$d": [["price", 1.0], ["qty", 1]]}, 3]}
    assert plug.decode(seq) == [{"price": 1.0, "qty": 1}, {"price": 2.0, "qty": 2}, {"price": 3.0, "qty": 3}]
    assert pin.plain(seq) == plug.decode(seq)
    assert pin.to_source({"$srep": ["a", 500]}) == "'a' * 500" and pin.to_source({"$srep": ["a", 3]}) == "'aaa'"
    rec = {"$new": {"m": "orders.repository", "n": "OrderRepository", "a": [":memory:"], "k": []}}
    assert pin.to_source(rec) == "OrderRepository(':memory:')"
    assert pin.imports_needed([rec]) == {"orders.repository": {"OrderRepository"}}


def test_constants_resolve_through_env_defaults_and_arithmetic():
    ev = lambda s: pin.const_eval(ast.parse(s, mode="eval").body)  # noqa: E731
    assert ev('int(os.environ.get("ORDERS_MAX_ITEMS", "50"))') == 50
    assert ev('float(os.getenv("T", "100.0"))') == 100.0
    assert ev("2**31 - 1") == 2**31 - 1 and ev("-5") == -5
    with pytest.raises(ValueError):
        ev("10**10000")
    with pytest.raises(ValueError):
        ev("open('x')")


def test_boundaries_are_mined_from_comparisons_lengths_slices_and_imported_constants(tmp_path):
    repo = _plain_repo(tmp_path, {"orders/extra.py": "from orders.config import MAX_ITEMS_PER_ORDER\n\n"
                                                    "LIMIT = 7\n\n\ndef f(xs: list, s: str, n: int) -> int:\n"
                                                    "    if len(xs) > MAX_ITEMS_PER_ORDER:\n        return -1\n"
                                                    "    if n <= LIMIT or s == 'admin':\n        return 0\n"
                                                    "    return round(sum(xs[:10]), 2)\n"})
    proj = probe._Project(repo, _files(repo))
    node = proj.node("orders/extra.py", "f")
    b = pin.mine(node, "orders/extra.py", proj.const_lookup("orders/extra.py"), pin.Bounds(),
                 lambda name: proj.const_source("orders/extra.py", name))
    assert 50 in b.lengths and 10 in b.lengths and "orders/config.py:6" in b.lengths[50]
    assert json.loads(next(k for k in b.numbers)) == 7 and "admin" in b.strings and b.rounding
    edges = [v for v, _ in pin.edges({"k": "int"}, b)]
    assert {6, 7, 8} <= set(edges)
    sizes = [pin.plain(v.enc) for v, t in pin.edges({"k": "list", "of": {"k": "int"}}, b)
             if t and t.startswith("length 50")]
    assert sorted(len(s) for s in sizes) == [49, 50, 51]


def test_the_corpus_is_deterministic_and_puts_boundaries_first():
    params = [{"name": "subtotal", "kind": "pos", "ann": None, "default": None}]
    b = pin.Bounds()
    b.add_number(100.0, "p.py:3 (THRESHOLD)")
    one = probe.build_corpus(params, [{"k": "float"}], b, [], [], 120, 0)
    two = probe.build_corpus(params, [{"k": "float"}], b, [], [], 120, 0)
    assert pin.corpus_sha256(one[0]) == pin.corpus_sha256(two[0])
    first = [c["a"][0] for c in one[0][:6]]
    assert 100.0 in first and math.nextafter(100.0, math.inf) in first
    assert len(one[0]) == 120 and one[2]["generated"] > 0
    other = probe.build_corpus(params, [{"k": "float"}], b, [], [], 120, 1)
    assert pin.corpus_sha256(other[0]) != pin.corpus_sha256(one[0])


def test_without_hypothesis_a_fixed_pseudo_random_list_fills_the_budget(monkeypatch):
    monkeypatch.setitem(sys.modules, "hypothesis", None)
    got, how = pin.generated([{"k": "int"}, {"k": "str"}], 20, 3)
    again, _ = pin.generated([{"k": "int"}, {"k": "str"}], 20, 3)
    assert len(got) == 20 and got == again and "hypothesis is not installed" in how


def test_defaults_are_exercised_by_leaving_them_out():
    params = [{"name": "amount", "kind": "pos", "ann": None, "default": None},
              {"name": "currency", "kind": "pos", "ann": None, "default": ast.Constant("EUR")}]
    cases, meta, _ = probe.build_corpus(params, [{"k": "float"}, {"k": "str"}], pin.Bounds(), [], [], 50, 0)
    assert {"a": [1.0], "k": []} in cases  # the call with the default
    assert any(c["k"] and c["k"][0][0] == "currency" or len(c["a"]) == 2 for c in cases)


# -- the side-effect gate ------------------------------------------------------------------------------

SIDE = {"orders/side.py": '''import os
import random
import shutil
import subprocess
import urllib.request
from pathlib import Path

SEEN = []
_CACHE = {}


def write_it(p: str) -> None:
    open(p, "w").write("x")


def write_path(p: str) -> None:
    Path(p).write_text("x")


def remove(p: str) -> None:
    shutil.rmtree(p)


def fetch(u: str) -> bytes:
    return urllib.request.urlopen(u).read()


def run(a: list) -> int:
    return subprocess.call(a)


def remember(x: int) -> int:
    SEEN.append(x)
    return len(SEEN)


def memo(k: str) -> int:
    _CACHE[k] = 1
    return 1


def setenv(v: str) -> None:
    os.environ["X"] = v


def reseed(n: int) -> float:
    random.seed(n)
    return random.random()


def reads(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def env_read() -> str:
    return os.environ.get("HOME", "")


def pure(x: int) -> int:
    return x * 2
''', "orders/importtime.py": '''import sqlite3

CONN = sqlite3.connect("app.db")


def lookup(x: int) -> int:
    return x
''', "orders/uses_importtime.py": '''from orders.importtime import lookup


def twice(x: int) -> int:
    return 2 * lookup(x)
'''}


@pytest.mark.parametrize("fn,kind", [("write_it", "file-write"), ("write_path", "file-write"),
                                     ("remove", "file-write"), ("fetch", "network"), ("run", "process"),
                                     ("remember", "global-state"), ("memo", "global-state"),
                                     ("setenv", "global-state"), ("reseed", "global-state")])
def test_the_gate_refuses_side_effects_and_names_the_line(tmp_path, fn, kind):
    repo = _plain_repo(tmp_path, SIDE)
    res = probe_gate.check(repo, _files(repo), [("orders/side.py", fn)])
    assert res["verdict"] == "refused", res
    assert kind in {r["kind"] for r in res["reasons"]}
    assert all(r["at"].startswith("orders/side.py:") and r["via"][0] == fn for r in res["reasons"])


@pytest.mark.parametrize("fn", ["reads", "env_read", "pure"])
def test_the_gate_lets_reads_and_pure_functions_run(tmp_path, fn):
    repo = _plain_repo(tmp_path, SIDE)
    assert probe_gate.check(repo, _files(repo), [("orders/side.py", fn)])["verdict"] == "passed"


def test_the_gate_follows_calls_parameters_and_import_time_statements(tmp_path):
    repo = _plain_repo(tmp_path, SIDE)
    files = _files(repo)
    res = probe_gate.check(repo, files, [("orders/service.py", "place_order")])
    assert res["verdict"] == "refused"
    first = res["reasons"][0]
    assert first["at"] == "orders/repository.py:17" and first["kind"] == "sql-write"
    assert first["via"] == ["place_order", "OrderRepository.save"]
    # a pure function in a module whose import connects to a database: refused at import
    res = probe_gate.check(repo, files, [("orders/uses_importtime.py", "twice")])
    assert res["verdict"] == "refused" and res["reasons"][0]["phase"] == "import"
    assert res["reasons"][0]["at"] == "orders/importtime.py:3"
    # config.py reads the environment at import: that is not a side effect
    assert probe_gate.check(repo, files, [("orders/pricing.py", "apply_discount")])["verdict"] == "passed"


# -- the plugin's audit classification -----------------------------------------------------------------

def test_the_plugin_classifies_side_effect_events():
    plug._S.phase = "call"
    try:
        assert plug._classify("open", ("x.txt", "r", os.O_RDONLY)) is None
        assert plug._classify("open", ("x.txt", "w", os.O_WRONLY | os.O_CREAT)) == "file-write"
        assert plug._classify("open", ("x.txt", None, os.O_RDWR)) == "file-write"
        assert plug._classify("socket.connect", (None, ("1.2.3.4", 80))) == "network"
        assert plug._classify("subprocess.Popen", ("ls", [], None, None)) == "process"
        assert plug._classify("sqlite3.connect", (":memory:",)) is None
        assert plug._classify("sqlite3.connect", ("app.db",)) == "db-connection"
        assert plug._classify("os.putenv", ("A", "b")) == "global-state"
        assert plug._classify("object.__setattr__", (int, "x", 1)) == "class-state"
        assert plug._classify("object.__setattr__", (int, "__doc__", 1)) is None
        assert plug._classify("import", ("json",)) is None
    finally:
        plug._S.phase = "idle"


def test_reprs_mask_what_differs_between_runs_of_the_same_code(monkeypatch):
    monkeypatch.setattr(plug._S, "run_root", str(Path("C:/tmp/verinoda-exp-1")))
    root = plug._S.run_root
    assert plug._norm(repr(os.path.join(root, "repo", "x.txt"))).startswith("'<run>")
    assert plug._norm(os.path.join(root, "repo")) == os.path.join("<run>", "repo")
    assert plug._norm("<orders.X object at 0x7f00ab>") == "<orders.X object at 0x?>"


# -- oracles ----------------------------------------------------------------------------------------------

def test_difference_classes():
    r = lambda text, t="builtins.float": {"r": text, "t": t}  # noqa: E731
    e = lambda t: {"e": t, "m": ""}  # noqa: E731
    assert probe.classify(r("100.0"), r("90.0"), True) == "value_changed_at_mined_boundary"
    assert probe.classify(r("100.0"), r("90.0"), False) == "value_changed"
    assert probe.classify(r("inf"), e("builtins.OverflowError"), False) == "new_exception"
    assert probe.classify(e("builtins.ValueError"), r("1"), False) == "exception_removed"
    assert probe.classify(e("builtins.ValueError"), e("builtins.KeyError"), False) == "exception_type_changed"
    assert probe.classify(e("builtins.ValueError"), e("builtins.ValueError"), False) is None
    assert probe.classify(r("50", "builtins.int"), r("50.0"), False) == "type_changed"
    assert probe.classify(r("4.901663330598706e+16"), r("4.901663330598705e+16"), False) == "numeric_drift"
    assert probe.classify(r("1000000000000000000", "builtins.int"), r("1000000000000000001", "builtins.int"),
                          False) == "value_changed"  # integers never drift
    assert probe.classify({**r("[]", "builtins.list"), "ap": "a"}, {**r("[]", "builtins.list"), "ap": "b"},
                          False) == "argument_mutation_changed"
    assert probe.classify({"hang": True}, r("1"), False) == "timeout_removed"
    assert probe.classify(r("1"), {"hang": True}, False) == "new_timeout"


def test_scaling_is_flagged_only_when_every_round_grows_three_times_faster():
    lin = {"rounds": [[1e-5, 1e-4, 1e-3]] * 3}
    quad = {"rounds": [[1e-5, 1e-3, 0.1]] * 3}
    assert probe._scaling_verdict(lin, quad)["flag"] is True
    assert probe._scaling_verdict(lin, lin)["flag"] is False
    noisy = {"rounds": [[1e-5, 1e-3, 0.1], [1e-5, 1e-4, 1e-3], [1e-5, 1e-3, 0.1]]}
    assert probe._scaling_verdict(lin, noisy)["flag"] is False
    assert probe._scaling_verdict(None, quad)["flag"] is None


def test_probe_evidence_is_graded_full_only_for_its_own_probe_input_and_class():
    meta = {"kind": "probe_counterexample", "probe_id": "prb_1", "class": "value_changed", "symbol": "a.py::f",
            "input_sha256": "abc", "outcome": "pass", "reproduced": True}
    ev = {"source_type": "experiment", "meta": meta}
    spec = {"probe": {"id": "prb_1", "class": "value_changed", "input": "abc", "symbol": "a.py::f"}}
    assert entail._probe_grade(spec, ev, ["a.py::f"]).grade == "full"
    assert entail._probe_grade({"probe": {**spec["probe"], "id": "prb_2"}}, ev, ["a.py::f"]).grade == "none"
    assert entail._probe_grade({"probe": {**spec["probe"], "input": "x"}}, ev, ["a.py::f"]).grade == "partial"
    assert entail._probe_grade(spec, {**ev, "meta": {**meta, "reproduced": False}}, ["a.py::f"]).grade == "partial"
    assert entail._probe_grade(spec, ev, ["b.py::g"]).grade == "none"
    summ = {"source_type": "experiment", "meta": {**meta, "kind": "probe_summary"}}
    assert entail._probe_grade({"probe": {"id": "prb_1", "class": "no_difference"}}, summ, []).grade == "partial"
    assert entail.typed("behaviour", spec)


def test_targets_are_resolved_and_unsafe_paths_refused(tmp_path):
    repo = _plain_repo(tmp_path)
    files = _files(repo)
    assert probe.resolve_target(repo, "orders/pricing.py::apply_discount", files) == \
        ("orders/pricing.py", "apply_discount")
    assert probe.resolve_target(repo, "./orders/pricing.py::apply_discount", files)[0] == "orders/pricing.py"
    assert probe.resolve_target(repo, "compute_total", files) == ("orders/pricing.py", "compute_total")
    assert probe.resolve_target(repo, "OrderRepository.save", files) == ("orders/repository.py",
                                                                          "OrderRepository.save")
    for bad in ("../x.py::f", "-x.py::f", "/abs/x.py::f", ".git/config::f", "orders/pricing.py::"):
        with pytest.raises(ValueError):
            probe.resolve_target(repo, bad, files)
    with pytest.raises(ValueError, match="no function"):
        probe.resolve_target(repo, "nope_nope", files)


# -- real runs ---------------------------------------------------------------------------------------------

@pytest.mark.experiment
@needs_git
def test_b1_boundary_truncation_and_new_exception_are_found_and_recorded(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", AD_OLD, "if subtotal >= DISCOUNT_THRESHOLD:\n"
                                            "        return int(subtotal * 0.9 * 100) / 100")
    before = _digest(repo)
    st = open_store(repo)
    res = probe.probe(st, repo, "orders/pricing.py::apply_discount", emit_test=True)
    assert res["status"] == "differences_found", res["headline"]
    classes = {d["class"]: d for d in res["differences"]}
    assert {"new_exception", "value_changed_at_mined_boundary"} <= set(classes)
    bnd = classes["value_changed_at_mined_boundary"]["examples"][0]
    assert bnd["call"] == "apply_discount(100.0)" and bnd["base"]["returns"] == "100.0" and \
        bnd["head"]["returns"] == "90.0"
    assert "orders/config.py:7" in bnd["why_this_input"][0]
    assert classes["new_exception"]["examples"][0]["head"]["raises"] == "builtins.OverflowError"
    assert all(d["reproduced"] for d in res["differences"])
    # run-scoped claims, experiment_verified, one per class
    for d in res["differences"]:
        assert d["claim_status"] == "experiment_verified", d
        c = st.claim(d["claim_id"])
        assert c["kind"] == "behaviour" and "In probe prb_" in c["text"] and "always" not in c["text"]
    # runs went through the experiment runner; the corpus is on disk with its hash
    exps = {r["id"] for r in st.all("SELECT id FROM experiments")}
    assert set(res["runs"]["base"] + res["runs"]["head"]) <= exps
    corpus = json.loads(Path(res["inputs"]["corpus_path"]).read_text(encoding="utf-8"))
    assert pin.corpus_sha256(corpus["cases"]) == res["inputs"]["corpus_sha256"] == corpus["corpus_sha256"]
    assert res["guarantees"]["fs_writes_confined"] is False and "not_checked" in res
    # the emitted test pins the base behaviour and parses; nothing was written to the repository
    ast.parse(res["regression_test"])
    assert "apply_discount(100.0) == 100.0" in res["regression_test"]
    assert _digest(repo) == before
    st.close()


@pytest.mark.experiment
@needs_git
def test_a_behaviour_preserving_refactor_reports_no_difference(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", AD_OLD + "\n    return subtotal",
         "return round(subtotal * 0.9, 2) if subtotal > DISCOUNT_THRESHOLD else subtotal")
    st = open_store(repo)
    res = probe.probe(st, repo, "apply_discount", seed=3)
    assert res["status"] == "no_difference_found", res
    assert res["differences"] == [] and "not a proof" in res["headline"]
    assert res["claim_status"] == "weak_inference"
    st.close()


@pytest.mark.experiment
@needs_git
def test_side_effects_are_refused_before_anything_runs_unless_the_user_allows(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/service.py", "    total = compute_total(items)\n    return repo.save(customer, total)",
         "    oid = None\n    for item in items:\n        oid = repo.save(customer, compute_total([item]))\n"
         "    return oid")
    st = open_store(repo)
    res = probe.probe(st, repo, "orders/service.py::place_order")
    assert res["status"] == "refused" and "orders/repository.py:17" in res["headline"]
    assert "place_order -> OrderRepository.save" in res["headline"]
    assert st.all("SELECT id FROM experiments") == []
    res = probe.probe(st, repo, "orders/service.py::place_order", allow_side_effects=True, inputs=60)
    assert res["gate"]["overridden_by_user"] is True
    assert res["status"] == "differences_found", res["headline"]
    ex = next(d for d in res["differences"] if d["class"] == "value_changed")["examples"][0]
    assert ex["base"]["returns"] == "1" and ex["head"]["returns"] != "1"
    assert "OrderRepository(':memory:')" in ex["call"]
    st.close()


@pytest.mark.experiment
@needs_git
def test_a_side_effect_the_gate_cannot_see_is_blocked_at_run_time(tmp_path):
    repo = _repo(tmp_path, {"orders/sneaky.py": "def wipe(p: str) -> int:\n"
                                                "    __import__('os').remove(p)\n    return 1\n"})
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    st = open_store(repo)
    res = probe.probe(st, repo, "orders/sneaky.py::wipe", examples=[repr((str(victim),))], inputs=20)
    assert res["status"] == "refused" and "at run time" in res["headline"], res
    assert "file-write" in res["blocked"]["example"]["event"]
    assert victim.read_text(encoding="utf-8") == "keep"
    st.close()


@pytest.mark.experiment
@needs_git
def test_an_import_time_side_effect_the_gate_cannot_see_is_blocked(tmp_path):
    outside = tmp_path / "made_at_import"
    repo = _repo(tmp_path, {"orders/boot.py": f"__import__('os').makedirs({str(outside)!r})\n\n\n"
                                              "def ok(x: int) -> int:\n    return x\n"})
    st = open_store(repo)
    res = probe.probe(st, repo, "orders/boot.py::ok", inputs=10)
    assert res["status"] == "refused" and "importing orders.boot" in res["headline"], res["headline"]
    assert not outside.exists()
    st.close()


@pytest.mark.experiment
@needs_git
def test_methods_with_recipes_static_methods_and_a_tree_without_git(tmp_path):
    extra = {"orders/money.py": "class Money:\n    def __init__(self, cents: int):\n        self.cents = cents\n\n"
                                "    def plus(self, other: int) -> int:\n        return self.cents + other\n\n"
                                "    @staticmethod\n    def parse(text: str) -> int:\n"
                                "        return int(text) if text.isdigit() else -1\n",
             "tests/test_money.py": "from orders.money import Money\n\n\ndef test_plus():\n"
                                    "    assert Money(5).plus(1) == 6\n"}
    repo = _repo(tmp_path, extra)
    _sub(repo, "orders/money.py", "return self.cents + other", "return self.cents + other if other else 0")
    st = open_store(repo)
    res = probe.probe(st, repo, "orders/money.py::Money.plus", inputs=40)
    assert res["status"] == "differences_found", res["headline"]
    assert res["differences"][0]["examples"][0]["call"] == "Money(5).plus(0)"
    res = probe.probe(st, repo, "orders/money.py::Money.parse", inputs=40)
    assert res["status"] == "no_difference_found", res["headline"]
    st.close()
    plain = _plain_repo(tmp_path)
    st = open_store(plain)
    res = probe.probe(st, plain, "orders/pricing.py::apply_discount", inputs=30)
    assert res["status"] == "nothing_found" and res["base"]["differential"] is False
    assert any("no base commit" in lim for lim in res["limits"])
    st.close()


@pytest.mark.experiment
@needs_git
def test_a_run_that_reaches_its_timeout_is_not_a_hang(tmp_path):
    repo = _repo(tmp_path, {"orders/slow.py": "import time\n\n\ndef nap(n: int) -> int:\n"
                                              "    time.sleep(0.25)\n    return n\n"})
    _sub(repo, "orders/slow.py", "return n", "return n + 1")
    st = open_store(repo)
    res = probe.probe(st, repo, "orders/slow.py::nap", inputs=40, timeout=4)
    assert res["status"] in ("inconclusive", "differences_found")
    assert "new_timeout" not in {d["class"] for d in res["differences"]} and not res.get("timeouts")
    assert any("reached its 4 s timeout" in lim or "not run" in lim for lim in res["limits"]), res["limits"]
    st.close()


@pytest.mark.experiment
@needs_git
def test_hangs_nondeterminism_properties_and_no_base(tmp_path):
    extra = {"orders/misc.py": "import random\n\n\ndef spin(n: int) -> int:\n    return n\n\n\n"
                               "def noisy(n: int) -> float:\n    return n + random.random()\n\n\n"
                               "def neg(x: int) -> int:\n    return -abs(x)\n"}
    repo = _repo(tmp_path, extra)
    _sub(repo, "orders/misc.py", "def spin(n: int) -> int:\n    return n",
         "def spin(n: int) -> int:\n    while n == 7:\n        pass\n    return n")
    st = open_store(repo)
    res = probe.probe(st, repo, "orders/misc.py::spin", per_call_timeout=0.5, inputs=40, examples=["(7,)"])
    # slow or not ending is performance: listed, never a difference without scaling
    assert res["status"] == "inconclusive" and res["differences"] == [], res["headline"]
    hang = next(d for d in res["timeouts"] if d["class"] == "new_timeout")
    assert hang["examples"][0]["call"] == "spin(7)" and hang["examples"][0]["head"] == {"timeout": True}
    assert hang["examples"][0]["base"]["returns"] == "7" and "not judged" in res["headline"]
    res = probe.probe(st, repo, "orders/misc.py::noisy", inputs=30)
    assert res["status"] in ("inconclusive", "no_difference_found") and res["differences"] == []
    assert res["nondeterministic"]["count"] >= 1
    res = probe.probe(st, repo, "orders/misc.py::neg", no_base=True, properties=["result >= 0"], inputs=30)
    assert res["status"] == "property_violated" and res["property_violations"][0]["count"] > 0
    assert res["base"]["differential"] is False
    with pytest.raises(ValueError, match="not a Python expression"):
        probe.probe(st, repo, "orders/misc.py::neg", no_base=True, properties=["result >="])
    st.close()


@pytest.mark.experiment
@needs_git
def test_unsupported_and_refused_arguments(tmp_path):
    repo = _repo(tmp_path, {"mod/Heat.kt": "object Heat { fun f(x: Int) = x }\n",
                            "orders/aio.py": "async def later(x: int) -> int:\n    return x\n"})
    st = open_store(repo)
    res = probe.probe(st, repo, "mod/Heat.kt::Heat.f")
    assert res["status"] == "unsupported" and "Kotlin" in res["headline"]
    assert probe.probe(st, repo, "orders/aio.py::later")["status"] == "unsupported"
    for bad in ("-x", "--output=/tmp/x", "HEAD\nx"):
        with pytest.raises(ValueError):
            probe.probe(st, repo, "orders/pricing.py::apply_discount", base=bad)
    st.close()


@pytest.mark.experiment
@needs_git
def test_cli_and_changed_functions(tmp_path, capsys):
    repo = _repo(tmp_path)
    _sub(repo, "orders/service.py", "len(items) > MAX_ITEMS_PER_ORDER", "len(items) >= MAX_ITEMS_PER_ORDER")
    rc = cli.main(["probe", "orders/service.py::validate_items", "--repo", str(repo), "--json", "--inputs", "80"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 3 and out["status"] == "differences_found"
    ex = next(d for d in out["differences"] if d["class"] == "new_exception")["examples"][0]
    assert ex["head"]["raises"] == "orders.service.ValidationError" and "50 items" in ex["call"]
    assert probe.changed_functions(repo, _git(repo, "rev-parse", "HEAD").strip()) == \
        [{"symbol": "orders/service.py::validate_items", "change": "body", "line": 12}]
    rc = cli.main(["probe", "--changed", "--repo", str(repo), "--inputs", "60"])
    text = capsys.readouterr().out
    assert rc == 3 and "1 changed Python function" in text and "validate_items" in text
    rc = cli.main(["probe", "orders/pricing.py::apply_discount", "--repo", str(repo), "--inputs", "40"])
    text = capsys.readouterr().out
    assert rc == 0 and text.startswith("apply_discount: no behaviour difference found")


@pytest.mark.experiment
@needs_git
def test_mcp_change_probe(tmp_path):
    from verinoda.mcp.server import AtlasTools

    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", "subtotal > DISCOUNT_THRESHOLD", "subtotal >= DISCOUNT_THRESHOLD")
    open_store(repo).close()
    t = AtlasTools(repo)
    res = t.change_probe(symbol="orders/pricing.py::apply_discount", inputs=60)
    assert res["status"] == "differences_found" and res["differences"][0]["examples"]
    assert t.change_probe()["error"] == "invalid_argument"
    assert t.change_probe(symbol="x.py::f", changed=True)["error"] == "invalid_argument"
    listing = t.change_probe(changed=True, inputs=40)
    assert listing["probes"][0]["symbol"] == "orders/pricing.py::apply_discount"
