"""Precise call-site resolution with jedi (DESIGN D29): kinds, verdicts, cache, budget.

The fixture project reproduces the two error classes the research found in
Graphify's own call edges - a shadowed duplicate definition (``_nfc``) and an
import-alias collision - next to the cases that must *not* be definitive:
calls through parameters/locals and methods on non-self receivers.
Skipped when the optional ``precise`` extra (jedi) is not installed.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import pytest  # noqa: E402

from repoatlas import precise  # noqa: E402
from repoatlas.store import Store  # noqa: E402

jedi = pytest.importorskip("jedi", reason="optional extra 'precise' (jedi) not installed")

MOD = (
    "import os\n"                                   # 1
    "from pkg.other import helper as h2\n"          # 2
    "from pkg import other\n"                       # 3
    "\n"                                            # 4
    "\n"                                            # 5
    "def _nfc(s):\n"                                # 6
    "    return s\n"                                # 7
    "\n"                                            # 8
    "\n"                                            # 9
    "def _nfc(s):  # shadows the first one\n"       # 10
    "    return s.upper()\n"                        # 11
    "\n"                                            # 12
    "\n"                                            # 13
    "class Repo:\n"                                 # 14
    "    def __init__(self):\n"                     # 15
    "        self.rows = []\n"                      # 16
    "\n"                                            # 17
    "    def save(self, x):\n"                      # 18
    "        return self.flush(x)\n"                # 19
    "\n"                                            # 20
    "    def flush(self, x):\n"                     # 21
    "        return x\n"                            # 22
    "\n"                                            # 23
    "\n"                                            # 24
    "def logged(fn):\n"                             # 25
    "    return fn\n"                               # 26
    "\n"                                            # 27
    "\n"                                            # 28
    "@logged\n"                                     # 29
    "def decorated():\n"                            # 30
    "    return 1\n"                                # 31
    "\n"                                            # 32
    "\n"                                            # 33
    "def use(repo: Repo, cb, items):\n"             # 34
    "    a = _nfc('x')\n"                           # 35
    "    repo.save(1)\n"                            # 36
    "    cb(2)\n"                                   # 37
    "    h2(3)\n"                                   # 38
    "    os.path.join('a', 'b')\n"                  # 39
    "    r = Repo()\n"                              # 40
    "    other.helper(4)\n"                         # 41
    "    f = make()\n"                              # 42
    "    f()\n"                                     # 43
    "    sorted(items, key=_nfc)\n"                 # 44
    "    s = 'ğüşİı'; decorated()\n"                # 45
    "    return a\n"                                # 46
    "\n"                                            # 47
    "\n"                                            # 48
    "def make():\n"                                 # 49
    "    def inner():\n"                            # 50
    "        return 1\n"                            # 51
    "    return inner\n"                            # 52
)
OTHER = "def helper(x):\n    return x\n\n\ndef h2(x):  # same name as the alias in mod.py\n    return x\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_bytes(b"")
    (root / "pkg" / "mod.py").write_bytes(MOD.encode("utf-8"))
    (root / "pkg" / "other.py").write_bytes(OTHER.encode("utf-8"))
    (root / "pkg" / "notes.txt").write_bytes(b"not code\n")
    precise.reset_caches()
    yield root
    precise.reset_caches()


def _r(repo, line, label, tp=None, tl=None, **kw):
    return precise.resolve_call(repo, "pkg/mod.py", line, label, target_path=tp, target_line=tl, **kw)


def test_shadowed_duplicate_definition_is_refuted_and_the_live_one_confirmed(repo):
    shadowed = _r(repo, 35, "_nfc", "pkg/mod.py", 6)
    assert shadowed["kind"] == "definitive" and shadowed["verdict"] == "refutes"
    assert [(t["path"], t["line"], t["type"]) for t in shadowed["targets"]] == [("pkg/mod.py", 10, "function")]
    live = _r(repo, 35, "_nfc", "pkg/mod.py", 10)
    assert live["verdict"] == "confirms" and live["receiver"] == "name"


def test_import_alias_resolves_to_the_real_function_not_a_same_named_one(repo):
    via_alias = _r(repo, 38, "helper", "pkg/other.py", 1)
    assert via_alias["kind"] == "definitive" and via_alias["verdict"] == "confirms" and via_alias["alias"] == "h2"
    # A name collision: a graph edge from h2(3) to other.h2 (same name, different function) is wrong.
    collision = _r(repo, 38, "h2", "pkg/other.py", 5)
    assert collision["kind"] == "definitive" and collision["verdict"] == "refutes"
    assert collision["targets"][0]["path"] == "pkg/other.py" and collision["targets"][0]["line"] == 1


def test_module_attribute_and_self_calls_are_definitive(repo):
    mod = _r(repo, 41, "helper", "pkg/other.py", 1)
    assert mod["kind"] == "definitive" and mod["receiver"] == "module" and mod["verdict"] == "confirms"
    me = _r(repo, 19, "flush", "pkg/mod.py", 21)
    assert me["kind"] == "definitive" and me["receiver"] == "self" and me["verdict"] == "confirms"


def test_dynamic_calls_are_never_definitive_or_refuting(repo):
    param = _r(repo, 37, "cb", "pkg/other.py", 1)
    assert param["kind"] == "dynamic" and param["verdict"] == "undetermined" and "parameter" in param["reason"]
    local = _r(repo, 43, "f", "pkg/mod.py", 49)
    assert local["kind"] == "dynamic" and local["verdict"] == "undetermined"
    assert [t["line"] for t in local["targets"]] == [50]  # the inferred candidate is still reported
    # a method on a parameter receiver: jedi's static answer is a candidate, not a binding
    method = _r(repo, 36, ".save()", "pkg/other.py", 1)
    assert method["kind"] == "dynamic" and method["receiver"] == "parameter" and method["verdict"] == "undetermined"
    assert method["targets"][0]["line"] == 18
    # referenced, not called (a callback passed to C code)
    ref = _r(repo, 44, "_nfc", "pkg/mod.py", 6)
    assert ref["kind"] == "dynamic" and ref["site"] == "reference" and ref["verdict"] == "undetermined"


def test_external_unresolved_and_constructor_cases(repo):
    ext = _r(repo, 39, "join", "pkg/other.py", 1)
    assert ext["kind"] == "external" and ext["verdict"] == "undetermined" and ext["targets"][0]["in_repo"] is False
    none = _r(repo, 39, "not_called_here")
    assert none["kind"] == "unresolved" and "no call" in none["reason"]
    cls = _r(repo, 40, "Repo", "pkg/mod.py", 14)
    assert cls["kind"] == "definitive" and cls["verdict"] == "confirms"
    init = _r(repo, 40, ".__init__()", "pkg/mod.py", 15)
    assert init["verdict"] == "confirms" and init["ctor"] is True and init["targets"][0]["init_line"] == 15
    # Class(...) whose own __init__ is not the target: maybe inherited - never a refutation
    other_init = _r(repo, 40, "__init__", "pkg/other.py", 1)
    assert other_init["verdict"] == "undetermined"


def test_decorator_line_targets_and_non_ascii_columns(repo):
    # the graph may place a decorated function on its decorator line; the call is after non-ASCII text
    dec = _r(repo, 45, "decorated", "pkg/mod.py", 29)
    assert dec["kind"] == "definitive" and dec["verdict"] == "confirms"
    assert dec["col"] == len("    s = 'ğüşİı'; ")  # characters, not UTF-8 bytes


def test_results_are_cached_by_file_hash_and_invalidated_by_edits(repo):
    st = Store(":memory:")
    first = _r(repo, 35, "_nfc", store=st)
    assert first["cached"] is False and first["tool"].startswith("jedi ")
    rows = st.all("SELECT * FROM resolutions")
    assert len(rows) == 1 and rows[0]["path"] == "pkg/mod.py" and rows[0]["line"] == 35 and len(rows[0]["file_sha256"]) == 64
    precise.reset_caches()  # a new process: the SQLite cache answers
    again = _r(repo, 35, "_nfc", store=st)
    assert again["cached"] is True and again["targets"] == first["targets"]
    # editing the file changes its sha256: a fresh answer, a new cache row (old rows stay)
    p = repo / "pkg" / "mod.py"
    p.write_bytes(p.read_bytes().replace(b"def _nfc(s):  # shadows", b"def _nfc_old(s):  # renamed"))
    edited = _r(repo, 35, "_nfc", "pkg/mod.py", 6, store=st)
    assert edited["cached"] is False and edited["verdict"] == "confirms"
    assert st.one("SELECT COUNT(*) AS n FROM resolutions")["n"] == 2


def test_budget_bounds_fresh_work_but_not_cached_answers(repo):
    b = precise.Budget(max_sites=1, max_seconds=60)
    assert _r(repo, 35, "_nfc", budget=b) is not None
    assert _r(repo, 38, "h2", budget=b) is None  # spent: no answer, current behaviour stays
    assert b.skipped == 1 and b.sites == 1
    assert _r(repo, 35, "_nfc", budget=b)["cached"] is True  # a cached answer costs nothing
    assert b.as_dict()["skipped"] == 1
    cfg = repo / ".repoatlas" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text('{"budget": {"precise_sites": 3, "precise_seconds": 0.5}}', encoding="utf-8")
    fb = precise.Budget.from_config(repo)
    assert (fb.max_sites, fb.max_seconds) == (3, 0.5)
    zero = precise.Budget(max_sites=10, max_seconds=0)
    assert _r(repo, 38, "h2", budget=zero) is None and zero.skipped == 1


def test_no_answer_without_jedi_or_outside_the_repo(repo, monkeypatch):
    assert precise.resolve_call(repo, "../outside.py", 1, "x") is None
    assert precise.resolve_call(repo, "pkg/missing.py", 1, "x") is None
    assert precise.resolve_call(repo, "pkg/notes.txt", 1, "x") is None  # not Python, no SCIP index
    monkeypatch.setattr(precise, "available", lambda: (False, "jedi is not installed"))
    precise.reset_caches()
    assert _r(repo, 35, "_nfc") is None
    assert precise.resolve_file(repo, "pkg/mod.py") == []


def test_resolve_file_covers_every_call_site(repo):
    res = precise.resolve_file(repo, "pkg/other.py")
    assert res == []  # no calls in other.py
    res = precise.resolve_file(repo, "pkg/mod.py")
    kinds = {(r["line"], r["token"]): r["kind"] for r in res}
    assert kinds[(35, "_nfc")] == "definitive" and kinds[(37, "cb")] == "dynamic"
    assert kinds[(39, "join")] == "external" and len(res) >= 12


def test_target_token_and_call_sites_helpers():
    assert precise.target_token(".save()") == "save"
    assert precise.target_token("OrderRepository.save") == "save"
    assert precise.target_token("apply_discount()") == "apply_discount"
    import ast

    src = "x = a.b.c(1)\ny = (f\n     .g())\n"
    tree, lines = ast.parse(src), src.splitlines()
    assert [(s.token, s.receiver, s.col) for s in precise.call_sites(tree, lines, 1)] == [("c", "attribute", 8)]
    assert [(s.token, s.line) for s in precise.call_sites(tree, lines, 3)] == [("g", 3)]
    assert [(s.token, s.line) for s in precise.call_sites(tree, lines, 2)] == [("g", 3)]  # call starts on 2
    assert precise.KINDS == ("definitive", "dynamic", "ambiguous", "unresolved", "external")
    ok, why = precise.available()
    assert ok and why.startswith("jedi ")


def test_ambiguous_when_a_name_has_several_definitions(tmp_path):
    root = tmp_path / "amb"
    root.mkdir()
    (root / "m.py").write_bytes(b"import sys\n\nif sys.platform == 'win32':\n    def f():\n        return 1\n"
                                b"else:\n    def f():\n        return 2\n\n\ndef g():\n    return f()\n")
    precise.reset_caches()
    r = precise.resolve_call(root, "m.py", 12, "f", target_path="m.py", target_line=4)
    assert r["kind"] == "ambiguous" and r["verdict"] == "undetermined"
    assert sorted(t["line"] for t in r["targets"]) == [4, 7]
    precise.reset_caches()


def test_resolution_evidence_is_anchored_on_the_call_line(repo):
    from repoatlas import evidence as evmod

    res = _r(repo, 35, "_nfc", "pkg/mod.py", 6)
    ev = precise.resolution_evidence(repo, res, commit="c0")
    assert ev["source_type"] == "static_resolution" and ev["path"] == "pkg/mod.py" and ev["line_start"] == 35
    assert ev["commit_sha"] == "c0" and ev["excerpt"].strip() == "a = _nfc('x')"
    m = ev["meta"]
    assert m["kind"] == "definitive" and m["verdict"] == "refutes" and m["tool"].startswith("jedi ")
    assert m["targets"][0]["line"] == 10 and m["targets"][0]["def_hash"].startswith("sha256:")
    assert evmod.check_source(repo, ev).ok
    assert "-> definitive" in ev["locator"]
