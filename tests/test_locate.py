"""`verinoda locate` and `verinoda coupled` (verinoda/locate.py): the files a change touches besides the first one.

The coupled signals are tested on temporary git repositories built here (commits that change files together, a
.c/.h pair, arch-variant twins, a shallow clone, a folder that is not git); `locate` and the worker on a copy of
examples/orders_app that is indexed once."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from verinoda import cli, locate, workflow
from verinoda.mcp.server import AtlasTools
from verinoda.store import open_store

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "orders_app"


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, env=env)


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _commit(repo: Path, msg: str, files: dict[str, str]) -> None:
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


TREE = {
    "src/kernel/boot.c": '#include "kernel/machine.h"\n#include <config.h>\n#include <kernel/boot.h>\nvoid boot(void) {}\n',
    "include/kernel/boot.h": '#include "machine.h"\nvoid boot(void);\n',
    "include/kernel/machine.h": "void machine(void);\n",
    "include/a/config.h": "#define A 1\n",
    "include/b/config.h": "#define B 1\n",
    "src/other.c": "void other(void) {}\n",
    "src/arch/arm/kernel/thread.c": "void thread_arm(void) {}\n",
    "src/arch/x86/kernel/thread.c": "void thread_x86(void) {}\n",
    "pkg/__init__.py": "",
    "pkg/a.py": "from .b import f\n\n\ndef g():\n    return f()\n",
    "pkg/b.py": "def f():\n    return 1\n",
    "pkg/c.py": "from . import a\n",
    "pkg/util.py": "x = 1\n",
    "pkg/util.pyi": "x: int\n",
    "tests/test_util.py": "def test_x():\n    pass\n",
}


@pytest.fixture(scope="module")
def proj(tmp_path_factory) -> Path:
    """boot.c and machine.h change together in 5 commits; boot.c alone in 1; with other.c in 1; two bulk commits
    (35 files) change boot.c and gen/x0.c."""
    repo = tmp_path_factory.mktemp("coupled") / "kern"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "init", TREE)
    for i in range(5):
        _commit(repo, f"together {i}", {"src/kernel/boot.c": TREE["src/kernel/boot.c"] + f"/* {i} */\n",
                                        "include/kernel/machine.h": f"void machine(void); /* {i} */\n"})
    _commit(repo, "alone", {"src/kernel/boot.c": TREE["src/kernel/boot.c"] + "/* alone */\n"})
    _commit(repo, "with other", {"src/kernel/boot.c": TREE["src/kernel/boot.c"] + "/* o */\n",
                                 "src/other.c": "void other(void) { }\n"})
    for i in range(2):
        bulk = {f"gen/x{n}.c": f"int x{n} = {i};\n" for n in range(35)}
        bulk["src/kernel/boot.c"] = TREE["src/kernel/boot.c"] + f"/* bulk {i} */\n"
        _commit(repo, f"bulk {i}", bulk)
    return repo


def _paths(res: dict) -> list[str]:
    return [f["path"] for f in res["files"]]


def _rel(res: dict, path: str) -> list[dict]:
    return next(f for f in res["files"] if f["path"] == path)["relations"]


# -- cochange: the anchor's own commits, then their whole file lists ---------------------------------------------

def test_cochange_reads_the_anchors_commits_and_lists_the_files_changed_with_them(proj):
    res = locate.coupled(proj, ["src/kernel/boot.c"], max_files=20)
    assert res["history"]["read"] is True and res["history"]["anchors_read"] == 1 and "reason" not in res["history"]
    assert res["history"]["commits"] == 10  # init, 5 together, alone, other, 2 bulk
    co = [r for r in _rel(res, "include/kernel/machine.h") if r["kind"] == "cochange"]
    # 8 commits of at most 30 files (the two bulk commits are left out): machine.h is in init and the 5 together
    assert co and co[0]["anchor"] == "src/kernel/boot.c" and co[0]["shared"] == 6 and co[0]["commits"] == 8
    assert co[0]["degree"] == round(6 / 8, 3)
    row = next(f for f in res["files"] if f["path"] == "include/kernel/machine.h")
    assert row["tier"] == "coupled" and row["score"] == round(6 / 8, 3) and "6 of 8 commits" in row["why"]
    assert "src/kernel/boot.c" not in _paths(res)  # never coupled to itself
    assert not any(p.startswith("gen/") for p in _paths(res))  # bulk commits are not evidence
    assert [r["shared"] for r in _rel(res, "src/other.c") if r["kind"] == "cochange"] == [2]  # init and "with other"
    assert "src/arch/arm/kernel/thread.c" not in _paths(res)  # in the first commit only: one shared commit


def test_a_file_needs_two_shared_commits_and_fifteen_percent(proj, monkeypatch):
    res = locate.coupled(proj, ["src/other.c"], max_files=20)  # other.c: init and "with other" (2 commits)
    shared = {f["path"]: f for f in res["files"]}
    assert [r["shared"] for r in shared["src/kernel/boot.c"]["relations"] if r["kind"] == "cochange"] == [2]  # 2 of 2
    assert "include/kernel/machine.h" not in shared  # one shared commit with other.c: init
    monkeypatch.setattr(locate, "MIN_DEGREE", 0.3)  # other.c is 2 of boot.c's 8 commits: 25 %
    res = locate.coupled(proj, ["src/kernel/boot.c"], max_files=20)
    assert "include/kernel/machine.h" in _paths(res) and "src/other.c" not in _paths(res)


def test_the_two_step_reading_is_the_reason_machine_h_is_found(proj):
    """One `git log --name-only -- boot.c` lists only boot.c: the second command reads the whole file lists."""
    one = subprocess.run(["git", "log", "--no-merges", "--format=%x01", "--name-only", "--", "src/kernel/boot.c"],
                         cwd=proj, capture_output=True, text=True, check=True).stdout
    assert "machine.h" not in one
    assert "include/kernel/machine.h" in _paths(locate.coupled(proj, ["src/kernel/boot.c"], max_files=20))


def test_history_ignores_git_environment_of_another_repository(proj, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))  # what `git bisect run` leaves in a child's environment
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    res = locate.coupled(proj, ["src/kernel/boot.c"])
    assert res["history"]["read"] is True and "include/kernel/machine.h" in _paths(res)


def test_a_repository_walked_far_back_is_cut_at_the_window(proj, monkeypatch):
    monkeypatch.setattr(locate, "WINDOW", 3)
    monkeypatch.setattr(locate, "_long_history", lambda gitdir: True)  # a fresh repository has no packs to judge by
    res = locate.coupled(proj, ["src/kernel/boot.c"], max_files=20)
    assert res["history"]["read"] is True and res["history"]["window"] == 3
    assert res["history"]["commits"] <= 3
    assert "last 3 commits only" in locate.render(res, kind="coupled")


# -- neighbours, pairs, twins ------------------------------------------------------------------------------------

def test_python_relative_imports_forward_and_reverse(proj):
    res = locate.coupled(proj, ["pkg/a.py"], history=False, max_files=20)
    assert res["history"] == {"read": False, "commits": 0, "anchors_read": 0, "reason": "disabled (--no-history)"}
    assert {"kind": "neighbour", "anchor": "pkg/a.py", "how": "imported"} in _rel(res, "pkg/b.py")  # a imports b
    assert {"kind": "neighbour", "anchor": "pkg/a.py", "how": "imports"} in _rel(res, "pkg/c.py")  # c imports a


def test_c_includes_resolve_to_one_file_only(proj):
    res = locate.coupled(proj, ["src/kernel/boot.c"], history=False, max_files=20)
    # "kernel/machine.h" and <kernel/boot.h> end exactly one path each; <config.h> is include/a and include/b: skipped
    assert {"kind": "neighbour", "anchor": "src/kernel/boot.c", "how": "included"} in _rel(res, "include/kernel/machine.h")
    assert {"kind": "neighbour", "anchor": "src/kernel/boot.c", "how": "included"} in _rel(res, "include/kernel/boot.h")
    assert not any("config.h" in p for p in _paths(res))


def test_reverse_includes_stay_inside_the_anchors_folder(proj):
    res = locate.coupled(proj, ["include/kernel/machine.h"], history=False, max_files=20)
    assert {"kind": "neighbour", "anchor": "include/kernel/machine.h", "how": "includes"} in _rel(res, "include/kernel/boot.h")
    # src/kernel/boot.c includes it too, but from outside include/kernel/: not a neighbour
    assert "src/kernel/boot.c" not in _paths(res)


def test_pairs_by_stem_and_twins_by_name(proj):
    pair = locate.coupled(proj, ["src/kernel/boot.c"], history=False, max_files=20)
    assert {"kind": "pair", "anchor": "src/kernel/boot.c"} in _rel(pair, "include/kernel/boot.h")
    py = locate.coupled(proj, ["pkg/util.py"], history=False)
    assert _paths(py) == ["pkg/util.pyi"] and py["files"][0]["relations"][0]["kind"] == "pair"
    twin = locate.coupled(proj, ["src/arch/x86/kernel/thread.c"], history=False)
    assert _paths(twin) == ["src/arch/arm/kernel/thread.c"]
    assert twin["files"][0]["why"] == "twin of src/arch/x86/kernel/thread.c" and twin["files"][0]["score"] is None
    # tests are not partners: tests/test_util.py shares no stem, and a generic name has no twins
    assert "pkg/__init__.py" not in _paths(locate.coupled(proj, ["pkg/__init__.py"], history=False))


def test_order_is_cochange_then_neighbour_then_pair_then_twin_and_the_cap_holds(proj):
    res = locate.coupled(proj, ["src/kernel/boot.c"], max_files=20)
    kinds = [min(locate.KINDS.index(r["kind"]) for r in f["relations"]) for f in res["files"]]
    assert kinds == sorted(kinds)
    capped = locate.coupled(proj, ["src/kernel/boot.c"], max_files=1)
    assert len(capped["files"]) == 1 and capped["truncated"] is True and capped["files"][0]["relations"][0]["kind"] == "cochange"


def test_an_anchor_missing_from_the_repository_is_reported(proj):
    res = locate.coupled(proj, ["src/kernel/boot.c", "nope/missing.c", "/elsewhere/x.c", "../x.c"], history=False)
    assert res["missing_anchors"] == ["nope/missing.c", "/elsewhere/x.c", "../x.c"] or sorted(
        res["missing_anchors"]) == sorted(["nope/missing.c", "/elsewhere/x.c", "../x.c"])
    assert res["anchors"] == ["src/kernel/boot.c"]


# -- degrading: shallow clone, not git ---------------------------------------------------------------------------

def test_a_shallow_clone_says_so_and_still_lists_the_code_signals(proj, tmp_path):
    clone = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", proj.as_uri(), str(clone))
    res = locate.coupled(clone, ["src/arch/x86/kernel/thread.c"])
    assert res["history"]["read"] is False and res["history"]["reason"] == "history unavailable: shallow clone"
    assert _paths(res) == ["src/arch/arm/kernel/thread.c"]
    assert "history unavailable: shallow clone" in locate.render(res, kind="coupled")


def test_a_folder_that_is_not_git_still_has_pairs_and_twins(tmp_path):
    plain = tmp_path / "plain"
    for rel, text in TREE.items():
        _write(plain, rel, text)
    res = locate.coupled(plain, ["src/arch/x86/kernel/thread.c", "pkg/util.py"])
    assert res["history"]["reason"] == "history unavailable: not a git repository" and res["history"]["read"] is False
    assert set(_paths(res)) == {"src/arch/arm/kernel/thread.c", "pkg/util.pyi"}


def test_a_git_call_over_the_budget_degrades_without_raising(proj, monkeypatch):
    real = locate._run

    def slow(repo, args, deadline, stdin=None):
        return (None, "timeout") if args[0] == "log" else real(repo, args, deadline, stdin)

    monkeypatch.setattr(locate, "_run", slow)
    res = locate.coupled(proj, ["src/arch/x86/kernel/thread.c"], time_budget_s=1.0)
    assert res["history"]["read"] is False and "time budget" in res["history"]["reason"]
    assert _paths(res) == ["src/arch/arm/kernel/thread.c"]


# -- the command line --------------------------------------------------------------------------------------------

def test_coupled_json_shape_and_exit_code(proj, capsys):
    assert cli.main(["coupled", "src/kernel/boot.c", "--repo", str(proj), "--json", "--max-files", "2"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert set(res) == {"anchors", "files", "history", "missing_anchors", "truncated", "seconds", "text"}
    assert res["text"].startswith("verinoda coupled: 2 files") and "include/kernel/machine.h" in res["text"]  # what a model reads
    assert len(res["files"]) == 2 and res["truncated"] is True
    f = res["files"][0]
    assert set(f) == {"path", "tier", "why", "score", "relations"} and f["tier"] == "coupled"
    assert res["history"]["read"] is True and isinstance(res["history"]["commits"], int)


def test_coupled_text_is_compact(proj, capsys):
    assert cli.main(["coupled", "src/kernel/boot.c", "--repo", str(proj)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("verinoda coupled: ") and "history: 10 commits read" in out and len(out) <= 1800
    assert "  include/kernel/machine.h  - changed together in 6 of 8 commits with src/kernel/boot.c" in out.replace(
        " (also neighbour)", "")
    assert cli.main(["coupled", "src/kernel/boot.c", "--repo", str(proj), "--no-history"]) == 0
    assert "disabled (--no-history)" in capsys.readouterr().out


def test_locate_command_needs_text_or_serve(proj, capsys):
    with pytest.raises(SystemExit):
        cli.main(["locate", "--repo", str(proj)])


def test_render_drops_files_from_the_end_to_fit_the_budget(proj):
    res = locate.coupled(proj, ["src/kernel/boot.c"], max_files=20)
    full = locate.render(res, 10**6, kind="coupled")
    cut = locate.fit(json.loads(json.dumps(res)), 260, kind="coupled")
    text = locate.render(cut, 260, kind="coupled")
    assert len(full) > 260 >= len(text) and len(cut["files"]) < len(res["files"]) and cut["truncated"] is True
    assert text.splitlines()[0].startswith("verinoda coupled: ")


# -- locate and the worker, end to end on a copy of examples/orders_app ------------------------------------------

@pytest.fixture(scope="module")
def orders(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("orders") / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    for i in range(3):
        for rel in ("orders/pricing.py", "tests/test_pricing.py"):
            with open(repo / rel, "ab") as fh:
                fh.write(f"\n# change {i}\n".encode())
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"change {i}")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return repo


TEXT = "the discount is not applied below the threshold when an order is placed"


def test_locate_lists_likely_files_then_files_that_change_with_them(orders):
    res = locate.locate(locate.GraphKeeper(orders), orders, TEXT)
    assert set(res) == {"question", "files", "anchors", "history", "freshness", "truncated", "seconds"}
    likely = [f for f in res["files"] if f["tier"] == "likely"]
    assert likely[0]["path"] == "orders/pricing.py" and likely[0]["lines"] and likely[0]["symbol"] == "apply_discount"
    assert "discount" in likely[0]["why"] and 2 <= len(likely) <= 3
    assert not any(f["path"].startswith("tests/") for f in likely)  # tests are not likely unless the text names them
    coupled_rows = [f for f in res["files"] if f["tier"] == "coupled"]
    assert [f["path"] for f in coupled_rows] == ["tests/test_pricing.py"]  # changed with pricing.py in 4 of 4 commits
    assert res["history"]["read"] is True and res["freshness"]["stale_count"] == 0
    named = locate.locate(locate.GraphKeeper(orders), orders, TEXT + " (see tests/test_pricing.py)")
    assert "tests/test_pricing.py" in [f["path"] for f in named["files"] if f["tier"] == "likely"]


def test_locate_text_budget_and_explicit_anchors(orders, capsys):
    assert cli.main(["locate", TEXT, "--repo", str(orders), "--max-chars", "260", "--anchor", "orders/config.py"]) == 0
    out = capsys.readouterr().out
    assert len(out) <= 261 and out.startswith("verinoda locate: ")
    assert cli.main(["locate", TEXT, "--repo", str(orders), "--json", "--max-chars", "260"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["truncated"] is True and len(locate.render(res, 10**6)) <= 260
    full = locate.locate(locate.GraphKeeper(orders), orders, TEXT, anchors=["orders/config.py"], history=False)
    assert "orders/config.py" in full["anchors"] and full["history"]["reason"] == "disabled (--no-history)"


def test_mcp_tools_answer_in_text_and_json(orders):
    t = AtlasTools(orders)
    loc = t.locate(TEXT)
    assert loc["format"] == "text" and loc["text"].startswith("verinoda locate: ") and "orders/pricing.py" in loc["text"]
    assert t.locate(TEXT, format="json")["files"][0]["tier"] == "likely"
    cp = t.coupled(["orders/pricing.py"], format="json")
    assert cp["files"][0]["path"] == "tests/test_pricing.py"
    assert t.coupled([])["error"] == "invalid_argument"
    assert t.locate("  ")["error"] == "invalid_argument"


class _Worker:
    def __init__(self, repo: Path):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.proc = subprocess.Popen([sys.executable, "-m", "verinoda", "locate", "--serve", "--repo", str(repo)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                                     cwd=repo)
        self._dog = threading.Timer(120, self.proc.kill)  # a worker that hangs fails the test, not the run
        self._dog.start()

    def line(self) -> dict:
        raw = self.proc.stdout.readline()
        assert raw.endswith(b"\n") and not raw.endswith(b"\r\n"), raw
        return json.loads(raw.decode("utf-8"))

    def send(self, text: str) -> None:
        self.proc.stdin.write(text.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def close(self) -> int:
        self.proc.stdin.close()
        code = self.proc.wait(timeout=60)
        self._dog.cancel()
        self.proc.stdout.close()
        return code


def test_locate_json_carries_the_text_a_model_reads(orders, capsys):
    assert cli.main(["locate", TEXT, "--repo", str(orders), "--json", "--max-chars", "600"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["text"].startswith("verinoda locate: ") and "orders/pricing.py" in res["text"] and len(res["text"]) <= 600
    assert res["files"][0]["path"] == "orders/pricing.py"
    assert cli.main(["locate", TEXT, "--repo", str(orders), "--max-chars", "600"]) == 0
    assert capsys.readouterr().out.strip() == res["text"]  # the same text without --json


def test_the_worker_answers_one_json_line_per_request_and_survives_a_bad_line(orders):
    w = _Worker(orders)
    try:
        ready = w.line()
        assert ready["ready"] is True and isinstance(ready["seconds"], float)
        w.send(json.dumps({"id": 1, "op": "locate", "text": TEXT, "anchors": [], "max_chars": 1800}))
        a = w.line()
        assert a["id"] == 1 and a["ok"] is True and a["result"]["files"][0]["path"] == "orders/pricing.py"
        w.send("this is not json")
        bad = w.line()
        assert bad["ok"] is False and bad["id"] is None and "not JSON" in bad["error"]
        w.send(json.dumps({"id": 2, "op": "coupled", "files": ["orders/pricing.py"]}))
        b = w.line()
        assert b["id"] == 2 and b["ok"] is True and b["result"]["files"][0]["path"] == "tests/test_pricing.py"
        w.send(json.dumps({"id": 3, "op": "nope"}))
        c = w.line()
        assert c["id"] == 3 and c["ok"] is False and "unknown op" in c["error"]
        w.send(json.dumps({"id": 4, "op": "coupled", "files": []}))
        assert w.line()["ok"] is False
        w.send("[1, 2]")
        assert w.line()["ok"] is False
        w.send(json.dumps({"id": 5, "op": "locate", "text": TEXT}))  # still alive after every bad line
        assert w.line()["ok"] is True
    finally:
        code = w.close()
    assert code == 0  # end of input ends it


def test_one_walk_for_several_anchors_counts_what_a_walk_for_one_counts(proj):
    import time

    both = locate._history_pass(proj, ["src/kernel/boot.c", "include/kernel/machine.h"], False, None,
                                time.monotonic() + 30)
    alone = locate._history_pass(proj, ["include/kernel/machine.h"], False, None, time.monotonic() + 30)
    assert both["include/kernel/machine.h"]["commits"] == alone["include/kernel/machine.h"]["commits"] == 6
    assert both["include/kernel/machine.h"]["shared"] == alone["include/kernel/machine.h"]["shared"]


# -- what an independent review found ----------------------------------------------------------------------------

def test_a_git_call_over_its_deadline_ends_with_everything_it_started(tmp_path):
    """On Windows `git` is often a shim (Git for Windows' cmd/git.exe) that starts the real git: ending the shim alone left the real one
    running to its end, and the deadline did nothing. The alias below sleeps for 6 s."""
    _git(tmp_path, "init", "-q")
    t0 = __import__("time").monotonic()
    out, why = locate._run(tmp_path, ["-c", "alias.slow=!sleep 6", "slow"], t0 + 0.6)
    took = __import__("time").monotonic() - t0
    assert out is None and why == "timeout" and took < 4.0, took


def test_anchors_beyond_the_twelve_read_are_told_as_unused_and_not_as_missing(tmp_path):
    repo = tmp_path / "many"
    repo.mkdir()
    _git(repo, "init", "-q")
    names = [f"m{i}.c" for i in range(14)]
    _commit(repo, "init", {n: f"int {n[:-2]};\n" for n in names})
    res = locate.coupled(repo, names)
    assert res["missing_anchors"] == [] and res["unused_anchors"] == ["m12.c", "m13.c"]
    assert "2 anchors beyond the 12 read were not used: m12.c, m13.c" in locate.render(res, 1800, kind="coupled")
    assert "unused_anchors" not in locate.coupled(repo, names[:3])  # only said when there is something to say
    res = locate.coupled(repo, [*names[:2], "../elsewhere.c"])
    assert res["missing_anchors"] == ["../elsewhere.c"] and "unused_anchors" not in res
