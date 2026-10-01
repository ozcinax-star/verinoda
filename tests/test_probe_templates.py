"""Property test templates of ``verinoda probe --emit-test --template`` (roundtrip, idempotent, equivalence).

The unit tests check the plan, the path rules, the observation and the rendered file without running anything.
The tests marked ``experiment`` probe a small codec module in a git copy of examples/orders_app end to end and
run the written file with pytest.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import ast  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, probe  # noqa: E402
from verinoda import probe_templates as ptpl  # noqa: E402
from verinoda.runtime import probe_plugin as plug  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

CODEC = '''"""A tiny codec for the template tests."""


def encode(text: str) -> str:
    return text[::-1]


def decode(text: str) -> str:
    return text[::-1]


def lossy_decode(text: str) -> str:
    return text[::-1].strip()


def squash(text: str) -> str:
    return " ".join(text.split())


def shout(text: str) -> str:
    return text + "!"


def scale(x: int, factor: int = 2) -> int:
    return x * factor


def ident(text: str) -> str:
    return text


def picky(text: str) -> str:
    if not (text.isascii() and text.isalnum()):
        raise ValueError("bad")
    return text


def uniq(words: list[str]) -> list[str]:
    return list(set(words))
'''


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path) -> Path:
    dst = tmp_path / "oa"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    (dst / "codec.py").write_bytes(CODEC.encode("utf-8"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _run_pytest(repo: Path, rel: str, seed: str | None = None, cwd: Path | None = None
                ) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST") and k != "PYTHONHASHSEED"}
    if seed is not None:
        env["PYTHONHASHSEED"] = seed
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", rel], cwd=cwd or repo,
                          capture_output=True, text=True, env=env, timeout=300)


P1 = [{"name": "text", "kind": "pos", "ann": None, "default": None}]


# -- plan, path, observation, rendering (no runs) ---------------------------------------------------------

def test_plan_builds_the_property_and_the_names_the_plugin_binds():
    rt = ptpl.plan("roundtrip", "encode", "function", P1, "codec", ("codec", "decode", "codec.py"), True)
    assert rt["property"] == "decode(result) == text" and rt["refs"] == {"decode": "codec"}
    assert rt["check"] == "decode(result) == args[0]" and rt["inverse"] == "codec.py::decode"
    idem = ptpl.plan("idempotent", "squash", "function", P1, "codec", None, False)
    assert idem["property"] == "squash(result) == result" and idem["refs"] == {"squash": "codec"}
    two = P1 + [{"name": "factor", "kind": "pos", "ann": None, "default": ast.Constant(2)}]
    sc = ptpl.plan("idempotent", "scale", "function", two, "codec", None, False)
    assert sc["check"] == "scale(result, *args[1:], **kwargs) == result"
    assert sc["shown"] == "scale(result, *rest) == result" and "__args__" in sc["property"]  # internals not shown
    eq = ptpl.plan("equivalence", "squash", "function", P1, "codec", None, True)
    assert eq["property"] is None and eq["refs"] == {}


@pytest.mark.parametrize("args, match", [
    (("bogus", "f", "function", P1, "m", None, True), "unknown template"),
    (("idempotent", "C.f", "method", P1, "m", None, True), "instance method"),
    (("roundtrip", "f", "function", P1, "m", None, True), "inverse"),
    (("idempotent", "f", "function", P1, "m", ("m", "g", "m.py"), True), "--inverse is for"),
    (("equivalence", "f", "function", P1, "m", None, False), "no base"),
    (("idempotent", "f", "function", [{"name": "x", "kind": "kwonly", "ann": None, "default": None}], "m", None,
      True), "required positional"),
    (("idempotent", "text", "function", P1, "m", None, True), "named 'text'"),
])
def test_plan_refuses_a_kind_that_does_not_fit(args, match):
    with pytest.raises(ValueError, match=match):
        ptpl.plan(*args)


def test_check_path_never_writes_over_a_file_and_stays_in_the_repository(tmp_path):
    (tmp_path / "tests").mkdir()
    assert ptpl.default_path(tmp_path, "C.norm", "idempotent") == "tests/test_c_norm_idempotent.py"
    assert ptpl.check_path(tmp_path, "tests/test_new.py") == "tests/test_new.py"
    (tmp_path / "tests" / "test_old.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="never written over"):
        ptpl.check_path(tmp_path, "tests/test_old.py")
    with pytest.raises(ValueError, match="outside the repository"):
        ptpl.check_path(tmp_path, "../elsewhere.py")
    with pytest.raises(ValueError, match="not a .py"):
        ptpl.check_path(tmp_path, "tests/notes.txt")
    for hidden in (".git/x.py", ".git/hooks/x.py", ".verinoda/t.py", "tests/.cache/t.py"):
        with pytest.raises(ValueError, match="hidden folder"):
            ptpl.check_path(tmp_path, hidden)
    with pytest.raises(FileExistsError):
        ptpl.write(tmp_path, "tests/test_old.py", "y = 2\n")
    assert (tmp_path / "tests" / "test_old.py").read_text(encoding="utf-8") == "x = 1\n"


def _rows(outs):
    return {i: {"x": [o, o]} for i, o in enumerate(outs)}


def test_observe_counts_counterexamples_first_and_never_says_verified():
    tmpl = {"kind": "idempotent", "property": "f(result) == result"}
    cases = [{"a": [v], "k": []} for v in ("a", "b", "c", "d")]
    rows = _rows([{"r": "'a'"}, {"r": "'b'", "pv": [0]}, {"e": "builtins.ValueError"}, {"r": "'d'", "pe": [[0, "x"]]}])
    obs, chosen = ptpl.observe(tmpl, 0, cases, rows, {}, set(), set())
    assert obs["status"] == "did_not_hold" and obs["evaluated"] == 3 and obs["counterexamples"] == 2
    assert chosen == [1, 3, 0] and obs["property_raised"] == 1 and "(it raised on 1)" in obs["observed"]
    ok, _ = ptpl.observe(tmpl, 0, cases[:1], _rows([{"r": "'a'"}]), {}, set(), set())
    assert ok["status"] == "observed_to_hold" and "not a verification" in ok["observed"]
    assert "verified" not in ok["observed"].replace("verification", "")
    none, chosen = ptpl.observe(tmpl, 0, cases[:1], _rows([{"e": "builtins.ValueError"}]), {}, set(), set())
    assert none["status"] == "not_evaluated" and chosen == []


def test_a_property_that_raises_is_a_counterexample_and_one_that_cannot_import_is_not_evaluated():
    tmpl = {"kind": "roundtrip", "property": "g(result) == text"}
    cases = [{"a": [v], "k": []} for v in ("a", "b c")]
    raised = _rows([{"r": "'a'"}, {"r": "'b c'", "pe": [[0, "ValueError: bad"]]}])
    obs, chosen = ptpl.observe(tmpl, 0, cases, raised, {}, set(), set())
    assert obs["status"] == "did_not_hold" and obs["counterexamples"] == 1 and chosen == [1, 0]
    missing = _rows([{"r": "'a'", "pe": [[0, "ModuleNotFoundError: No module named 'dec'", "import"]]}] * 2)
    obs, chosen = ptpl.observe(tmpl, 0, cases, missing, {}, set(), set())
    assert obs["status"] == "not_evaluated" and "No module named 'dec'" in obs["observed"] and chosen == []
    # the plugin marks an import failure of the functions a property calls
    props = plug._properties({"properties": ["g(result) == 1"]})
    assert plug._check_properties(props, ["x"], [1], {}, 1, {"g": "no_such_module_xyz"})[1][0][2] == "import"


def test_holds_at_base_only_when_the_property_was_evaluated_there():
    assert probe._at_base(0, {"x": [{"r": "1", "pe": [[0, "ModuleNotFoundError: x", "import"]]}]}) == {}
    assert probe._at_base(0, {"x": [{"e": "builtins.ValueError"}]}) == {}
    assert probe._at_base(0, None) == {}
    assert probe._at_base(0, {"x": [{"r": "1"}]}) == {"holds_at_base": True}
    assert probe._at_base(0, {"x": [{"r": "1", "pv": [0]}]}) == {"holds_at_base": False}


def test_an_input_whose_outcome_changes_with_the_hash_seed_is_left_out(tmp_path):
    tmpl = ptpl.plan("equivalence", "uniq", "function", P1, "codec", None, True)
    tmpl.update(path="test_uniq_equivalence.py", roots=["."])
    cases = [{"a": [["a"]], "k": []}, {"a": [["a", "b"]], "k": []}]
    rows = _rows([{"r": "['a']"}, {"r": "['a', 'b']"}])
    seen = []

    def rerun(spec, seed):
        seen.append(seed)
        return {"experiments": [f"exp_{seed}"], "rows": _rows([{"r": "['a']"}, {"r": "['b', 'a']"}])}
    runs = {"base": ["exp_b"], "head": ["exp_h"]}
    out = probe._property_test(tmp_path, tmpl, cases, rows, rows, set(), set(), {"module": "codec"}, "uniq", "prb_1",
                               runs, rerun)
    assert seen == list(probe.HASH_SEEDS) and out["hash_order_dropped"] == 1 and out["inputs_in_file"] == 1
    assert out["written"] and out["claim_status"] == "weak_inference" and runs["hash_seed"][0] == "exp_1"
    text = (tmp_path / "test_uniq_equivalence.py").read_text(encoding="utf-8")
    assert "_args(['a'])" in text and "_args(['a', 'b'])" not in text and "PYTHONHASHSEED" in text


def test_observe_equivalence_compares_both_sides_and_skips_results_without_plain_text():
    tmpl = {"kind": "equivalence", "property": None}
    cases = [{"a": [v], "k": []} for v in (1, 2, 3)]
    head = _rows([{"r": "1"}, {"r": "5"}, {"r": "<x at 0x...>", "np": ["masked"]}])
    base = _rows([{"r": "1"}, {"r": "2"}, {"r": "<x at 0x...>", "np": ["masked"]}])
    obs, chosen = ptpl.observe(tmpl, None, cases, head, base, set(), {1})
    assert obs["status"] == "did_not_hold" and obs["evaluated"] == 3 and chosen == [1, 0]
    assert obs["observed"] == "the working tree did not return or raise what the base did on 1 of 3 generated inputs"
    ok, _ = ptpl.observe(tmpl, None, cases[:1], head, base, set(), set())
    assert ok["observed"].startswith("the working tree returned or raised what the base did on all 1 generated")


def test_rendered_files_parse_and_name_the_observation():
    cases = [{"a": ["ab "], "k": []}, {"a": [3], "k": [["factor", 1]]}]
    rt = ptpl.plan("roundtrip", "encode", "function", P1, "codec", ("codec", "decode", "codec.py"), True)
    obs = {"observed": "x held on all 1 generated inputs", "counterexamples": 0}
    text = ptpl.render(rt, obs, [0], cases, {}, "encode", "codec", "prb_1", {"head": ["exp_1"]}, 10)
    ast.parse(text)
    assert "from codec import decode, encode" in text and "lambda: _args('ab ')" in text
    assert "assert decode(result) == args[0]" in text and "never verified" in text
    rt.update(path="tests/deep/test_x.py", roots=["tools", "scripts"])
    text = ptpl.render(rt, obs, [0], cases, {}, "encode", "codec", "prb_1", {"head": ["exp_1"]}, 10)
    ast.parse(text)
    assert "parents[2]" in text and "for _root in ('tools', 'scripts'):" in text
    eq = ptpl.plan("equivalence", "scale", "function", P1, "codec", None, True)
    text = ptpl.render(eq, obs, [1], cases, _rows([{}, {"r": "3"}]), "scale", "codec", "prb_1",
                       {"base": ["exp_b"], "head": ["exp_h"]}, 10)
    ast.parse(text)
    assert "(lambda: _args(3, factor=1), ('value', '3', False))" in text and "def _probe_repr" in text


def test_a_module_the_file_cannot_import_by_name_is_refused():
    ptpl.check_importable("pkg/codec.py", "pkg.codec")
    for rel, mod in (("my-codec.py", "my-codec"), ("class.py", "class"), ("2x.py", "2x")):
        with pytest.raises(ValueError, match="cannot be imported by name"):
            ptpl.check_importable(rel, mod)


def test_plugin_binds_the_template_functions_in_the_property_namespace():
    props = plug._properties({"properties": ["loads(result) == obj", "__args__[0] == obj"]})
    pv, pe = plug._check_properties(props, ["obj"], [[1]], {}, "[1]", {"loads": "json"})
    assert pv == [] and pe == []
    pv, pe = plug._check_properties(props, ["obj"], [[1]], {}, "[1]", {"loads": "no_such_module_xyz"})
    assert pv == [] and [e[0] for e in pe] == [0, 1] and "ModuleNotFoundError" in pe[0][1]


def test_cli_template_goes_with_emit_test(tmp_path, capsys):
    for argv in (["probe", "f", "--template", "idempotent"], ["probe", "--changed", "--emit-test", "--template",
                                                              "idempotent"], ["probe", "f", "--inverse", "g"]):
        with pytest.raises(SystemExit) as info:
            cli.main([*argv, "--repo", str(tmp_path)])
        assert str(info.value).startswith("error:")


# -- real runs ---------------------------------------------------------------------------------------------

@pytest.mark.experiment
@needs_git
def test_roundtrip_is_observed_written_and_the_file_passes(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    res = probe.probe(st, repo, "codec.py::encode", emit_test=True, template="roundtrip", inverse="codec.py::decode",
                      inputs=40)
    st.close()
    pt = res["property_test"]
    assert pt["written"] and pt["status"] == "observed_to_hold", res
    assert pt["path"] == "tests/test_encode_roundtrip.py" and pt["inputs_in_file"] > 0
    assert pt["claim_status"] == "weak_inference" and pt["claim_id"]
    assert res["status"] in probe.PASS_STATUSES
    done = _run_pytest(repo, pt["path"])
    assert done.returncode == 0, done.stdout + done.stderr
    # never over an existing file
    st = open_store(repo)
    with pytest.raises(ValueError, match="never written over"):
        probe.probe(st, repo, "codec.py::encode", emit_test=True, template="roundtrip", inverse="codec.py::decode")
    st.close()


@pytest.mark.experiment
@needs_git
def test_a_roundtrip_that_loses_data_is_reported_and_the_file_fails(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    res = probe.probe(st, repo, "codec.py::encode", emit_test=True, template="roundtrip",
                      inverse="codec.py::lossy_decode", inputs=40, test_file="tests/test_lossy.py", record=False)
    st.close()
    pt = res["property_test"]
    assert res["status"] == "property_violated" and pt["status"] == "did_not_hold" and pt["counterexamples"] > 0
    assert pt["written"] and "did not hold" in probe.render(res)
    assert _run_pytest(repo, "tests/test_lossy.py").returncode == 1


@pytest.mark.experiment
@needs_git
def test_idempotent_and_equivalence_templates(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    res = probe.probe(st, repo, "codec.py::squash", emit_test=True, template="idempotent", inputs=40, record=False)
    assert res["property_test"]["status"] == "observed_to_hold" and res["property_test"]["written"], res
    assert _run_pytest(repo, res["property_test"]["path"]).returncode == 0
    bad = probe.probe(st, repo, "codec.py::shout", emit_test=True, template="idempotent", inputs=20, record=False)
    assert bad["property_test"]["status"] == "did_not_hold"
    p = repo / "codec.py"
    p.write_bytes(p.read_bytes().replace(b'" ".join(text.split())', b'" ".join(text.split(" "))'))
    eq = probe.probe(st, repo, "codec.py::squash", emit_test=True, template="equivalence", inputs=40, record=False)
    st.close()
    pt = eq["property_test"]
    assert pt["status"] == "did_not_hold" and pt["written"], eq
    text = (repo / pt["path"]).read_text(encoding="utf-8")
    assert "BASE version" in text
    assert _run_pytest(repo, pt["path"]).returncode == 1  # the working tree differs from the base on some inputs


@pytest.mark.experiment
@needs_git
def test_an_inverse_that_raises_is_a_counterexample(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    res = probe.probe(st, repo, "codec.py::ident", emit_test=True, template="roundtrip", inverse="codec.py::picky",
                      inputs=40, record=False)
    st.close()
    pt = res["property_test"]
    assert pt["status"] == "did_not_hold" and pt["property_raised"] > 0 and res["status"] == "property_violated", pt
    assert pt["claim_status"] == "experiment_verified"
    assert _run_pytest(repo, pt["path"]).returncode == 1


@pytest.mark.experiment
@needs_git
def test_equivalence_leaves_out_inputs_whose_order_depends_on_the_hash_seed(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    res = probe.probe(st, repo, "codec.py::uniq", emit_test=True, template="equivalence", inputs=40, record=False)
    st.close()
    pt = res["property_test"]
    assert pt["written"] and pt["hash_order_dropped"] > 0, pt
    for seed in ("0", "4", "5"):
        done = _run_pytest(repo, pt["path"], seed=seed)
        assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.experiment
@needs_git
def test_the_file_imports_from_other_roots_and_a_missing_base_inverse_is_not_called_held(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tools").mkdir()
    (repo / "tools" / "tcodec.py").write_bytes(CODEC.encode("utf-8"))
    (repo / "scripts").mkdir()
    (repo / "scripts" / "dec.py").write_bytes(b"def decode2(text: str) -> str:\n    return text[::-1]\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "roots")
    (repo / "scripts" / "dec.py").write_bytes(b"def decode2(text: str) -> str:\n    return text[::-1]\n\n\n"
                                              b"def lossy2(text: str) -> str:\n    return text[::-1].strip()\n")
    st = open_store(repo)
    res = probe.probe(st, repo, "tools/tcodec.py::encode", emit_test=True, template="roundtrip",
                      inverse="scripts/dec.py::decode2", inputs=20, record=False)
    pt = res["property_test"]
    assert pt["status"] == "observed_to_hold" and pt["written"], res
    done = _run_pytest(repo, pt["path"].split("/", 1)[1], cwd=repo / "tests")  # from tests/, as plain pytest would
    assert done.returncode == 0, done.stdout + done.stderr
    bad = probe.probe(st, repo, "tools/tcodec.py::encode", emit_test=True, template="roundtrip",
                      inverse="scripts/dec.py::lossy2", inputs=20, record=False, test_file="tests/test_lossy2.py")
    assert bad["status"] == "property_violated"
    assert all("holds_at_base" not in ex for v in bad["property_violations"] for ex in v["examples"])
    assert "at the base it held" not in probe.render(bad)
    (repo / "my-codec.py").write_bytes(CODEC.encode("utf-8"))
    try:
        with pytest.raises(ValueError, match="cannot be imported by name"):
            probe.probe(st, repo, "my-codec.py::squash", emit_test=True, template="idempotent", record=False)
    finally:
        st.close()
