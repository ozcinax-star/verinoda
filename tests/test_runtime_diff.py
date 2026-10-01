"""Runtime differences between a base commit and the working tree (``verinoda.runtime.rundiff``): the same
tests traced twice, calls, library calls, SQL, exceptions, routes and test outcomes compared."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.runtime import rundiff  # noqa: E402
from verinoda.runtime import trace as rt  # noqa: E402
from verinoda.store import open_store  # noqa: E402

BASE = {
    "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n',
    "app/__init__.py": "",
    "app/orders.py": (
        "import sqlite3\n"
        "\n"
        "\n"
        "def db():\n"
        "    c = sqlite3.connect(':memory:')\n"
        "    c.executescript('CREATE TABLE item (id INTEGER PRIMARY KEY, price INTEGER);')\n"
        "    c.executemany('INSERT INTO item VALUES (?, ?)', [(i, i * 10) for i in range(1, 7)])\n"
        "    return c\n"
        "\n"
        "\n"
        "def price(c, i):\n"
        "    return c.execute('SELECT price FROM item WHERE id = ?', (i,)).fetchone()[0]\n"
        "\n"
        "\n"
        "def total(c, ids):\n"
        "    out = 0\n"
        "    for i in ids:\n"
        "        out += price(c, i)\n"
        "    return out\n"
        "\n"
        "\n"
        "def check(n):\n"
        "    if n < 0:\n"
        "        raise ValueError('negative')\n"
        "    return n\n"
    ),
    "tests/test_orders.py": (
        "import pytest\n"
        "\n"
        "from app import orders\n"
        "\n"
        "\n"
        "def test_total():\n"
        "    assert orders.total(orders.db(), [1, 2, 3, 4, 5, 6]) == 210\n"
        "\n"
        "\n"
        "def test_check_refuses():\n"
        "    with pytest.raises(ValueError):\n"
        "        orders.check(-1)\n"
        "\n"
        "\n"
        "def test_check_passes():\n"
        "    assert orders.check(2) == 2\n"
    ),
}

HEAD_ORDERS = (
    "import json\n"
    "import sqlite3\n"
    "\n"
    "\n"
    "def db():\n"
    "    c = sqlite3.connect(':memory:')\n"
    "    c.executescript('CREATE TABLE item (id INTEGER PRIMARY KEY, price INTEGER);')\n"
    "    c.executemany('INSERT INTO item VALUES (?, ?)', [(i, i * 10) for i in range(1, 7)])\n"
    "    return c\n"
    "\n"
    "\n"
    "def price(c, i):\n"
    "    return c.execute('SELECT price FROM item WHERE id = ?', (i,)).fetchone()[0]\n"
    "\n"
    "\n"
    "def total(c, ids):\n"
    "    marks = ', '.join('?' for _ in ids)\n"
    "    return c.execute(f'SELECT SUM(price) FROM item WHERE id IN ({marks})', list(ids)).fetchone()[0]\n"
    "\n"
    "\n"
    "def audit(n):\n"
    "    return json.dumps({'n': n})\n"
    "\n"
    "\n"
    "def check(n):\n"
    "    audit(n)\n"
    "    if n < 0:\n"
    "        raise KeyError('negative')\n"
    "    return n\n"
)

pytestmark = [pytest.mark.experiment,
              pytest.mark.skipif(shutil.which("git") is None, reason="git not available")]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    repo = tmp_path_factory.mktemp("rundiff") / "shop"
    for rel, text in BASE.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "app" / "orders.py").write_bytes(HEAD_ORDERS.encode("utf-8"))  # the head: uncommitted edits
    st = open_store(repo)
    res = rundiff.observe_pair(st, repo, [], "HEAD", timeout=180)
    yield repo, st, res
    st.close()


def _whats(sec: dict, part: str) -> list[str]:
    return [i["what"] for i in sec[part]]


def test_both_runs_are_recorded_and_compared(pair):
    _repo, _st, d = pair
    assert "error" not in d, d
    assert d["base"]["ref"] == "HEAD" and d["base"]["complete"] and d["head"]["complete"]
    assert d["tests_compared"] == 3 and d["changed"] is True


def test_calls_added_and_removed(pair):
    d = pair[2]
    assert "app/orders.py::check -> app/orders.py::audit" in _whats(d["calls"], "added")
    assert "app/orders.py::total -> app/orders.py::price" in _whats(d["calls"], "removed")
    item = next(i for i in d["calls"]["added"] if i["what"].endswith("::audit"))
    assert item["status"] == "observed" and item["at"].startswith("app/orders.py:")
    assert item["tests"] and all(t.startswith("tests/test_orders.py::") for t in item["tests"])
    assert "not in run" in item["basis"]


def test_library_calls_added(pair):
    d = pair[2]
    assert "app/orders.py::audit -> json.dumps" in _whats(d["library_calls"], "added")


def test_sql_added_removed_and_the_n_plus_one_gone(pair):
    sql = pair[2]["sql"]
    added, removed = _whats(sql, "added"), _whats(sql, "removed")
    assert any(s.startswith("SELECT SUM(price) FROM item WHERE id IN") for s in added), added
    assert "SELECT price FROM item WHERE id = ?" in removed
    gone = next(i for i in sql["removed"] if i["what"] == "SELECT price FROM item WHERE id = ?")
    assert gone["executions"] == 6 and gone["at"] == "app/orders.py:12"
    assert sql["n_plus_one_removed"] == ["SELECT price FROM item WHERE id = ?"] and sql["n_plus_one_added"] == []


@pytest.mark.skipif(sys.version_info < (3, 12), reason="raises are recorded with sys.monitoring")
def test_exceptions_raised_in_project_code(pair):
    ex = pair[2]["exceptions"]
    assert "builtins.KeyError raised in app/orders.py::check" in _whats(ex, "added")
    assert "builtins.ValueError raised in app/orders.py::check" in _whats(ex, "removed")


def test_a_test_outcome_that_changed_names_the_exception(pair):
    rows = {t["test"]: t for t in pair[2]["test_outcomes"]}
    t = rows["tests/test_orders.py::test_check_refuses"]
    assert (t["base"], t["head"]) == ("passed", "failed")
    assert t["head_exception"].startswith("builtins.KeyError at app/orders.py:")
    assert "tests/test_orders.py::test_total" not in rows


def test_the_base_run_does_not_touch_the_test_map(pair):
    repo, st, d = pair
    run = rt.load_run(st, d["base"]["run_id"])
    h = run["header"] if isinstance(run["header"], dict) else json.loads(run["header"])
    assert h["ref"] == "HEAD" and run["commit_sha"] == d["base"]["commit"]
    assert "app/orders.py" in h["files_differ_from_worktree"]


def test_an_incomplete_run_makes_absence_unknown(pair):
    _repo, st, d = pair
    with st.tx() as conn:
        conn.execute("UPDATE runtime_runs SET complete = 0 WHERE id = ?", (d["base"]["run_id"],))
    try:
        again = rundiff.compare(st, pair[0], d["base"]["run_id"], d["head"]["run_id"])
    finally:
        with st.tx() as conn:
            conn.execute("UPDATE runtime_runs SET complete = 1 WHERE id = ?", (d["base"]["run_id"],))
    added = next(i for i in again["calls"]["added"] if i["what"].endswith("::audit"))
    removed = next(i for i in again["calls"]["removed"] if i["what"].endswith("::price"))
    assert added["status"] == "unknown" and "incomplete" in added["basis"]
    assert removed["status"] == "observed"  # absent from the head run, which is complete
    assert any("incomplete" in lim for lim in again["limits"])


def test_routes_reached_are_compared_through_the_route_table(pair):
    _repo, st, d = pair

    class Mapper:
        def callee(self, path, line, qual):
            return {"check": "n_check", "total": "n_total"}.get(qual)

    table = [{"methods": ["GET"], "path": "/check", "handler": "n_check", "at": "app/web.py:3"},
             {"methods": None, "path": "/total", "handler": "n_total", "at": "app/web.py:9"},
             {"methods": ["POST"], "path": "/never", "handler": "n_other", "at": "app/web.py:15"}]
    base, head = rundiff.RunView(st, d["base"]["run_id"], pair[0]), rundiff.RunView(st, d["head"]["run_id"], pair[0])
    rb, rh = rundiff._routes(base, Mapper(), table), rundiff._routes(head, Mapper(), table)
    assert set(rb) == set(rh) == {"GET /check", "ANY /total"}
    assert rh["GET /check"]["at"] == "app/web.py:3" and rh["GET /check"]["tests"]


def test_render_lists_the_changes(pair):
    text = "\n".join(rundiff.render(pair[2]))
    assert "calls added" in text and "+ app/orders.py::check -> app/orders.py::audit" in text
    assert "- SELECT price FROM item WHERE id = ? x6 @app/orders.py:12" in text
    assert "N+1 gone in head: SELECT price FROM item WHERE id = ?" in text
    assert "test tests/test_orders.py::test_check_refuses: passed -> failed (builtins.KeyError" in text


def test_a_base_that_does_not_exist_is_an_error(tmp_path):
    repo = tmp_path / "p"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    st = open_store(repo)
    try:
        d = rundiff.observe_pair(st, repo, [], "no-such-ref", timeout=120)
        assert "does not name a commit" in d["error"]
        assert "runtime diff: no-such-ref does not name a commit" in "\n".join(rundiff.render(d))
    finally:
        st.close()


def test_review_observe_lists_the_runtime_changes(pair, capsys):
    from verinoda import cli, workflow

    repo, st, _ = pair
    workflow.scan(st, repo)
    code = cli.main(["review", "--observe", "--repo", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code in (0, 1, 3), out
    rd = out["tests"]["observe"]["runtime_diff"]
    assert "app/orders.py::check -> app/orders.py::audit" in _whats(rd["calls"], "added")
    assert "SELECT price FROM item WHERE id = ?" in _whats(rd["sql"], "removed")
    assert rd["base"]["commit"] == out["base"]["commit"]
    cli.main(["review", "--observe", "--repo", str(repo)])
    text = capsys.readouterr().out
    assert "runtime diff: base run" in text and "+ app/orders.py::check -> app/orders.py::audit" in text


def test_observe_compare_on_the_command_line(pair, capsys):
    from verinoda import cli

    repo, _st, _ = pair
    assert cli.main(["observe", "tests/test_orders.py::test_total", "--compare", "HEAD", "--repo", str(repo),
                     "--json"]) in (0, 3)
    rd = json.loads(capsys.readouterr().out)["runtime_diff"]
    assert rd["tests_compared"] == 1 and "SELECT price FROM item WHERE id = ?" in _whats(rd["sql"], "removed")
    with pytest.raises(SystemExit, match="--compare nope"):
        cli.main(["observe", "--compare", "nope", "--repo", str(repo)])
