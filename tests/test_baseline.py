"""A baseline of known decision-guard violations (verinoda/baseline.py): old violations pass, new ones fail, the
baseline shrinks as they are fixed and grows only by the user's call."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import baseline, cli, guards, index, workflow  # noqa: E402
from verinoda import decisions as dm  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

LAYERS = "layers order=app/ui/**,app/db/**"
FILES = {
    "app/__init__.py": "",
    "app/ui/__init__.py": "",
    # an allowed edge (ui uses db), so the guard always has an edge to check
    "app/ui/views.py": "from app.db.other import keep\n\n\ndef show():\n    return keep(1)\n",
    "app/db/__init__.py": "",
    # an old violation: the data layer reaches up into the ui
    "app/db/store.py": "from app.ui.views import show\n\n\ndef save(x):\n    return x or show()\n",
    "app/db/other.py": "def keep(x):\n    return x\n",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _update(repo: Path) -> None:
    st = open_store(repo)
    try:
        workflow.update(st, repo)
    finally:
        st.close()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "base ğ line"
    for rel, text in FILES.items():
        _write(r, rel, text)
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    workflow.init(r)
    st = open_store(r)
    try:
        workflow.scan(st, r)
        dm.record(st, r, chosen="two layers", rationale="the ui sits on top", guards=[LAYERS])
    finally:
        st.close()
    return r


def _check(repo: Path) -> dict:
    return guards.check(repo, graph=index.load(repo))


def _ddir(repo: Path) -> Path:
    return dm.decisions_dir_source(repo)[0]


def test_old_violations_pass_new_ones_fail_and_fixed_ones_shrink_the_baseline(repo):
    res = _check(repo)
    assert res["exit"] == 1 and {v["at"] for v in res["violations"]} == {"app/db/store.py:1", "app/db/store.py:5"}
    with pytest.raises(baseline.BaselineError, match="user's call"):
        baseline.record(_ddir(repo), guards.check(repo, graph=index.load(repo), use_baseline=False),
                        statement=" ", today="2026-10-01")
    out = baseline.record(_ddir(repo), guards.check(repo, graph=index.load(repo), use_baseline=False),
                          statement="we fix the old ones later", today="2026-10-01")
    assert out["entries"] == 2
    data = json.loads(baseline.path(_ddir(repo)).read_text(encoding="utf-8"))
    assert data["statement"] == "we fix the old ones later" and {e["file"] for e in data["entries"]} == {
        "app/db/store.py"}
    res = _check(repo)
    assert res["exit"] == 0 and not res["violations"] and len(res["baselined"]) == 2
    assert res["baselined"][0]["status"] == "statically_verified"      # still a violation, still cited
    assert res["baseline"]["matched"] == 2 and res["baseline_fixed"] == []
    # lines moving (an edit above) keep their entries; a new violation fails
    _write(repo, "app/db/store.py", "# moved down\n" + FILES["app/db/store.py"])
    _write(repo, "app/db/other.py", "from app.ui.views import show\n\n\ndef keep(x):\n    return show()\n")
    _update(repo)
    res = _check(repo)
    assert res["exit"] == 1 and {v["at"] for v in res["violations"]} == {"app/db/other.py:1", "app/db/other.py:5"}
    assert {b["at"] for b in res["baselined"]} == {"app/db/store.py:2", "app/db/store.py:6"}
    # the baseline does not grow without --replace
    with pytest.raises(baseline.BaselineError, match="would gain 2"):
        baseline.record(_ddir(repo), guards.check(repo, graph=index.load(repo), use_baseline=False),
                        statement="accept", today="2026-10-01")
    # fixing a baselined violation: reported, then shrunk without anyone's words
    _write(repo, "app/db/other.py", FILES["app/db/other.py"])
    _write(repo, "app/db/store.py", "def save(x):\n    return x\n")
    _update(repo)
    res = _check(repo)
    assert res["exit"] == 0 and res["baseline"]["fixed"] == 2 and "--shrink" in res["next_step"], res["unknown"]
    assert baseline.shrink(_ddir(repo), res, today="2026-10-01")["removed"] == 2
    assert _check(repo)["baseline"]["entries"] == 0
    # the violation coming back is new again: a shrunk baseline does not hold it any more
    _write(repo, "app/db/store.py", FILES["app/db/store.py"])
    _update(repo)
    assert _check(repo)["exit"] == 1


def test_a_changed_line_is_a_new_violation_and_duplicates_are_counted(repo):
    baseline.record(_ddir(repo), guards.check(repo, graph=index.load(repo), use_baseline=False),
                    statement="known", today="2026-10-01")
    # the same call twice where the baseline holds it once: one is baselined, one fails
    _write(repo, "app/db/store.py", FILES["app/db/store.py"] + "\n\ndef again(x):\n    return x or show()\n")
    _update(repo)
    res = _check(repo)
    assert res["exit"] == 1 and [v["at"] for v in res["violations"]] == ["app/db/store.py:9"]
    # a changed line is not the baselined one
    _write(repo, "app/db/store.py", FILES["app/db/store.py"].replace("x or show()", "show() or x"))
    _update(repo)
    res = _check(repo)
    assert [v["at"] for v in res["violations"]] == ["app/db/store.py:5"] and res["baseline"]["fixed"] == 1


def test_an_unreadable_baseline_never_passes(repo):
    p = baseline.path(_ddir(repo))
    p.write_text("{not json", encoding="utf-8")
    res = _check(repo)
    assert res["exit"] == 1 and any(u["kind"] == "baseline" for u in res["unknown"])
    p.write_text(json.dumps({"verinoda-baseline": 1, "entries": [{"decision": "ADR-0001"}]}), encoding="utf-8")
    assert any("without decision, guard and file" in u["why"] for u in _check(repo)["unknown"])
    p.write_text(json.dumps({"entries": []}), encoding="utf-8")
    assert any("not a Verinoda baseline" in u["why"] for u in _check(repo)["unknown"])


def test_cli(repo, capsys):
    r = str(repo)
    assert cli.main(["decide", "baseline", "--repo", r]) == 0
    assert "no baseline" in capsys.readouterr().out
    assert cli.main(["decide", "baseline", "--record", "--repo", r]) == 2
    assert "--said" in capsys.readouterr().err
    assert cli.main(["decide", "baseline", "--record", "--said", "known, fix later", "--repo", r, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["entries"] == 2
    assert cli.main(["decide", "check", "--repo", r]) == 0
    capsys.readouterr()
    assert cli.main(["decide", "check", "--repo", r, "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["baselined"]) == 2
    assert cli.main(["decide", "baseline", "--repo", r]) == 0
    assert "2 still found, 0 fixed, 0 new" in capsys.readouterr().out
    assert cli.main(["decide", "baseline", "--shrink", "--repo", r]) == 0
    assert "baseline unchanged" in capsys.readouterr().out
    assert cli.main(["decide", "baseline", "--shrink", "--record", "--repo", r]) == 2
