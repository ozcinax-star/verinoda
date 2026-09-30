"""What-if refactoring (verinoda/whatif.py): a simulated move or rename reports the rule violations and the
dependency cycles it would add or remove, and edits nothing."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import cli, index, whatif, workflow  # noqa: E402
from verinoda import decisions as dm  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

LAYERS = "layers order=app/ui/**,app/core/**,app/db/**"
FILES = {
    "app/__init__.py": "",
    "app/ui/__init__.py": "",
    "app/ui/views.py": "from app.core.service import place\n\n\ndef show():\n    return place(1)\n",
    "app/core/__init__.py": "",
    "app/core/service.py": "from app.db.store import save\n\n\ndef place(x):\n    return save(x)\n",
    "app/db/__init__.py": "",
    "app/db/store.py": "def save(x):\n    return x\n",
    # no cycle yet: x uses a, b uses x; a merged into b closes x <-> b
    "cyc/__init__.py": "",
    "cyc/a.py": "def fa():\n    return 1\n",
    "cyc/x.py": "from cyc.a import fa\n\n\ndef fx():\n    return fa()\n",
    "cyc/b.py": "from cyc.x import fx\n\n\ndef fb():\n    return fx()\n",
    # a cycle now: p <-> q; merging them breaks it
    "loop/__init__.py": "",
    "loop/p.py": "from loop.q import fq\n\n\ndef fp(n):\n    return fq(n - 1) if n else 0\n",
    "loop/q.py": "from loop.p import fp\n\n\ndef fq(n):\n    return fp(n - 1) if n else 0\n",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = tmp_path_factory.mktemp("whatif") / "what if ğ"
    for rel, text in FILES.items():
        _write(r, rel, text)
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    workflow.init(r)
    st = open_store(r)
    try:
        workflow.scan(st, r)
        dm.record(st, r, chosen="three layers", rationale="ui on core on db", guards=[LAYERS])
    finally:
        st.close()
    return r


def _rec(repo: Path, *specs: str) -> dm.Decision:
    return dm.Decision(id="ADR-0009", number=9, title="t",
                       guards=[dm.parse_guard(s, repo, f"g{i}") for i, s in enumerate(specs, 1)])


def _run(repo: Path, moves: list[str], *specs: str) -> dict:
    return whatif.run(index.load(repo), moves, records=[_rec(repo, *specs)] if specs else [])


def _tree(repo: Path) -> dict[str, str]:
    return {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(repo.rglob("*"))
            if p.is_file() and not {".git", ".verinoda"} & set(p.relative_to(repo).parts)}


# -- the move plan -----------------------------------------------------------------------------------

FILE_LIST = ["app/ui/views.py", "app/db/store.py", "app/db/__init__.py", "lib/util.py"]


def test_a_move_maps_files_and_folders_and_lists_merges():
    m, rows, merges = whatif.plan(FILE_LIST, ["app/db=storage"])
    assert m == {"app/db/store.py": "storage/store.py", "app/db/__init__.py": "storage/__init__.py"}
    assert rows == [{"from": "app/db", "to": "storage", "kind": "folder", "files": 2}] and not merges
    m, rows, _ = whatif.plan(FILE_LIST, ["app/db/=lib/"])   # a trailing slash: into the folder
    assert m["app/db/store.py"] == "lib/db/store.py"
    m, _, _ = whatif.plan(FILE_LIST, ["app/ui/views.py=lib"])  # an existing folder: into it
    assert m == {"app/ui/views.py": "lib/views.py"}
    m, _, _ = whatif.plan(FILE_LIST, [".\\app\\ui\\views.py=app/ui/pages.py"])  # a rename, Windows separators
    assert m == {"app/ui/views.py": "app/ui/pages.py"}
    _, _, merges = whatif.plan(FILE_LIST, ["app/db/store.py=lib/util.py"])
    assert merges == [{"into": "lib/util.py", "files": ["app/db/store.py", "lib/util.py"]}]


@pytest.mark.parametrize("spec,why", [
    ("app/db", "OLD=NEW"), ("nowhere.py=x.py", "no file or folder"), ("app/db=../out", "inside it"),
    ("app/db=/abs", "inside it"), ("app/db=C:/x", "inside it"), ("app/db/store.py=app/db/store.py", "nothing"),
])
def test_a_bad_move_is_refused(spec, why):
    with pytest.raises(whatif.WhatIfError, match=why):
        whatif.plan(FILE_LIST, [spec])


def test_a_file_two_moves_would_move_is_refused():
    with pytest.raises(whatif.WhatIfError, match="earlier --move"):
        whatif.plan(FILE_LIST, ["app/db=storage", "app/db/store.py=x.py"])


# -- rules ------------------------------------------------------------------------------------------

def test_a_move_that_breaks_the_layers_reports_the_added_violation(repo):
    before = _tree(repo)
    res = _run(repo, ["app/db/store.py=app/ui/store.py"], LAYERS)
    ru = res["rules"]
    assert res["status"] == "adds" and ru["edge_guards"] == 1
    assert ru["violations_before"] == 0 and ru["violations_after"] >= 1
    added = {f["at"]: f for f in ru["added"]}
    # the line cited is where the code is now; the rule is judged on the new path
    assert added["app/core/service.py:1"]["level"] == "VIOLATED"
    assert added["app/core/service.py:1"]["status"] == "statically_verified"
    assert "app/ui/store.py" in added["app/core/service.py:1"]["why"]
    assert not ru["removed"]
    assert _tree(repo) == before  # nothing was moved or edited


def test_a_renamed_target_is_still_named_by_the_code_that_imports_it(repo):
    # the import names `store`; the simulated path says persist.py: the edge is still VIOLATED, not POSSIBLE
    res = _run(repo, ["app/db/store.py=app/ui/persist.py"], LAYERS)
    added = {f["at"]: f for f in res["rules"]["added"]}
    assert added["app/core/service.py:1"]["level"] == "VIOLATED"


def test_a_move_that_fixes_a_violation_reports_it_removed(repo):
    res = _run(repo, ["app/core/service.py=app/services/service.py"], "no_edge from=app/ui/** to=app/core/**")
    ru = res["rules"]
    assert ru["violations_before"] >= 1 and ru["violations_after"] == 0
    assert "app/ui/views.py:1" in {f["at"] for f in ru["removed"]}
    assert not ru["added"] and res["status"] == "removes"


def test_a_move_that_empties_a_layer_is_a_new_unknown(repo):
    res = _run(repo, ["app/db=lib/db"], LAYERS)
    ru = res["rules"]
    assert ru["unknown_new"] >= 1 and ru["unknown"][0]["new"] and "app/db/**" in ru["unknown"][0]["why"]
    assert res["status"] == "adds"


def test_without_edge_guards_only_the_cycles_are_compared(repo):
    res = _run(repo, ["app/db=lib/db"])
    assert res["rules"] == {"edge_guards": 0}
    assert any("no accepted edge guard" in lim for lim in res["limits"])


def test_other_guard_kinds_are_named_as_not_rechecked(repo):
    res = _run(repo, ["app/db=lib/db"], LAYERS, "only_in calls=sqlite3.connect allowed=app/db/store.py")
    assert res["rules"]["edge_guards"] == 1
    assert any("1 other check(s)" in lim for lim in res["limits"])


# -- cycles -----------------------------------------------------------------------------------------

def _cycle_sets(repo: Path) -> set[frozenset]:
    return {frozenset(c) for c in am.cycle_components(index.load(repo))[0]}


def test_the_fixture_has_the_one_cycle_it_means(repo):
    assert _cycle_sets(repo) == {frozenset({"loop/p.py", "loop/q.py"})}


def test_a_rename_leaves_the_cycles_as_they_are(repo):
    res = _run(repo, ["loop/p.py=loop/pp.py", "cyc=cyc2"])
    assert res["cycles"]["before"] == res["cycles"]["after"] == 1
    assert not res["cycles"]["added"] and not res["cycles"]["removed"] and res["status"] == "no_change"


def test_a_merge_that_closes_a_cycle_reports_it_added(repo):
    res = _run(repo, ["cyc/a.py=cyc/b.py"])
    cy = res["cycles"]
    assert res["merges"] == [{"into": "cyc/b.py", "files": ["cyc/a.py", "cyc/b.py"]}]
    assert [set(c["files"]) for c in cy["added"]] == [{"cyc/b.py", "cyc/x.py"}]
    c = cy["added"][0]
    assert c["status"] == "strong_inference"
    # each dependency cites the line where the code is now: x's import of a, b's import of x
    ats = {at for d in c["dependencies"] for at in d["at"]}
    assert "cyc/b.py:1" in ats and "cyc/x.py:1" in ats
    assert res["status"] == "adds"


def test_a_merge_that_breaks_a_cycle_reports_it_removed(repo):
    res = _run(repo, ["loop/p.py=loop/q.py"])
    cy = res["cycles"]
    assert cy["before"] == 1 and cy["after"] == 0
    assert [set(c["files"]) for c in cy["removed"]] == [{"loop/p.py", "loop/q.py"}]
    assert res["status"] == "removes"


def test_the_cycles_view_is_unchanged_by_the_refactor(repo):
    view = am.cycles(index.load(repo))
    assert view["cycles_total"] == 1 and view["cycles"][0]["files"] == ["loop/p.py", "loop/q.py"]


# -- the command ------------------------------------------------------------------------------------

def test_cli(repo, capsys):
    r = str(repo)
    before = _tree(repo)
    assert cli.main(["what-if", "--move", "app/db/store.py=app/ui/store.py", "--repo", r]) == 3
    out = capsys.readouterr().out
    assert "+ VIOLATED" in out and "nothing edited" in out
    assert cli.main(["what-if", "--move", "loop/p.py=loop/pp.py", "--repo", r, "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "no_change" and res["rules"]["edge_guards"] == 1
    assert cli.main(["what-if", "--move", "nowhere=x", "--repo", r, "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    assert _tree(repo) == before
