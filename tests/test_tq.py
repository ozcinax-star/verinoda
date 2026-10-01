"""Typed questions (verinoda/tq.py, `verinoda tq`, MCP `tq`): the frozen gold set, each type's yes / no / unknown,
the status rules (verify, INFERRED hops, stale files, absences), the shared batch context, budgets, bad questions,
determinism and the command's exit codes. The graph query's object form gives the same rows as its text."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, graphquery, tq, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "benchmarks" / "tq_gold"
QLANG = GOLD / "fixtures" / "qlang"
ORDERS = ROOT / "examples" / "orders_app"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def _scan(src: Path | dict, dst: Path) -> Path:
    if isinstance(src, dict):
        for rel, text in src.items():
            p = dst / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8", newline="\n")
    else:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__"))
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


@pytest.fixture(scope="module")
def repos(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("tq")
    _scan(QLANG, root / "qlang")
    _scan(ORDERS, root / "orders_app")
    return root


@pytest.fixture(scope="module")
def ql(repos) -> Path:
    return repos / "qlang"


def _score():
    spec = importlib.util.spec_from_file_location("tq_gold_score", GOLD / "score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _one(repo: Path, question, **kw) -> dict:
    return tq.ask(repo, [question], **kw)["answers"][0]


# -- the gold set ---------------------------------------------------------------------------------------

def test_the_gold_set_is_frozen():
    manifest = json.loads((GOLD / "MANIFEST.json").read_text(encoding="utf-8"))
    for name, sha in manifest["files"].items():
        assert hashlib.sha256((GOLD / name).read_bytes()).hexdigest() == sha, name
    for rel, sha in manifest["fixtures"].items():
        assert hashlib.sha256((GOLD / rel).read_bytes()).hexdigest() == sha, rel
    held = json.loads((GOLD / "held_out.json").read_text(encoding="utf-8"))
    assert len(held) == manifest["counts"]["held_out.json"]
    assert all(hashlib.sha256(c["id"].encode()).digest()[0] < 0x56 for c in held)


@pytest.mark.parametrize("split", ["held_out", "dev"])
def test_no_wrong_answer_at_a_verified_status_and_every_unknown_has_a_next_step(repos, split):
    res = _score().run(repos, split)
    assert res["wrong_verified"] == [], res["wrong_verified"]
    assert res["unknown_without_next"] == []
    hit, n = map(int, res["located"].split("/"))
    assert n > 0 and hit >= 0.95 * n   # a true answer cites the gold line
    # the wrong answers are dynamic-dispatch traps, never above weak_inference
    assert {w["status"] for w in res["wrong"]} <= {"weak_inference"}


# -- the batch context ---------------------------------------------------------------------------------

def test_a_shared_context_checks_freshness_and_reads_routes_once(ql, monkeypatch):
    from verinoda import cross_service, freshness

    calls = {"fresh": 0, "collect": 0}
    real_check, real_collect = freshness.check, cross_service.collect

    def check(*a, **k):
        calls["fresh"] += 1
        return real_check(*a, **k)

    def collect(*a, **k):
        calls["collect"] += 1
        return real_collect(*a, **k)

    monkeypatch.setattr(freshness, "check", check)
    monkeypatch.setattr(cross_service, "collect", collect)
    res = tq.ask(ql, ['route "POST /orders" save_order', 'route "GET /orders" read_orders',
                      "calls post_order create_order", "reaches chain1 leaf", "callers leaf",
                      "q 'match (h:handler) return h' as=count", "writes post_order orders"])
    assert calls == {"fresh": 1, "collect": 1}
    assert res["shared"] == {"fresh": 1, "routes": 1}
    assert [a["answer"] for a in res["answers"]] == [True, True, True, True, 2, 5, True]


# -- each type ------------------------------------------------------------------------------------------

def test_exists_and_which(ql):
    a = _one(ql, "exists save_order")
    assert (a["answer"], a["status"], a["at"]) == (True, "statically_verified", ["app/store.py:6"])
    assert _one(ql, "exists save_order", verify=False)["status"] == "strong_inference"
    no = _one(ql, "exists delete_order")
    assert no["answer"] is False and no["status"] == "strong_inference" and no["why"] and no["next"]
    w = _one(ql, "which save_order in app/store.py|app/service.py")
    assert w["answer"] == ["app/store.py"] and w["status"] == "statically_verified"
    none = _one(ql, "which leaf in app/store.py|app/web.py")
    assert none["answer"] == [] and none["status"] == "strong_inference" and none["other"] == ["rec.py:31"]


def test_calls_verify_raises_and_inferred_hops_stay_weak(ql):
    yes = _one(ql, "calls post_order create_order")
    assert (yes["answer"], yes["status"], yes["at"]) == (True, "statically_verified", ["app/web.py:11"])
    assert _one(ql, "calls post_order create_order", verify=False)["status"] == "strong_inference"
    weak = _one(ql, "calls persist Repo.save")
    assert weak["answer"] is True and weak["status"] == "weak_inference"
    assert weak["why"].startswith("INFERRED calls edge app/service.py:22")
    assert weak["next"] == "verinoda resolve-call app/service.py:22 save" and weak["enough"] is False
    # a depth-1 no: strong only when the AST has no such call and the name is not spelled
    no = _one(ql, "calls get_orders save_order")
    assert (no["answer"], no["status"]) == (False, "strong_inference") and "observe" in no["next"]
    deep = _one(ql, "calls post_order save_order depth=2")
    assert deep["answer"] is True and deep["at"] == ["app/web.py:11", "app/service.py:6"]
    # the graph has no pong -> ping edge, but the AST has the call: unknown, not no
    gap = _one(ql, "calls pong ping")
    assert gap["answer"] is None and gap["next"] == "verinoda resolve-call rec.py:10 ping"
    # a name spelled in the body (a call through a variable) keeps a no weak
    far = _one(ql, "reaches leaf chain1")
    assert (far["answer"], far["status"]) == (False, "weak_inference")


def test_route_is_bound_from_the_route_table(ql):
    a = _one(ql, 'route "POST /orders" save_order')
    assert a["answer"] is True and a["at"][0] == "app/web.py:9" and a["status"] == "statically_verified"
    # GET /orders has its own handler: the asked route's line is the one cited
    g = _one(ql, 'route "GET /orders" read_orders')
    assert g["at"][0] == "app/web.py:14"
    assert _one(ql, 'route "GET /orders" save_order')["answer"] is False
    missing = _one(ql, 'route "DELETE /orders" save_order')
    assert missing["answer"] is None and missing["next"] == "verinoda routes"
    assert "POST /orders" in missing["candidates"]


def test_writes_reads_callers_taint_and_q(ql):
    w = _one(ql, "writes post_order orders")
    assert w["answer"] is True and w["status"] == "weak_inference" and "app/store.py:7" in w["at"]
    assert _one(ql, "reads post_order orders")["answer"] is False
    nt = _one(ql, "writes post_order nosuchtable")
    assert nt["answer"] is None and "orders" in nt["candidates"]
    c = _one(ql, "callers check")
    assert (c["answer"], c["bound"], c["status"]) == (2, "at_least", "strong_inference")
    assert set(c["at"]) == {"app/service.py:5", "tests/test_service.py:5"}
    t = _one(ql, "taint request.args sql")
    assert t["answer"] is True and t["at"] == ["app/admin.py:12", "app/admin.py:13"]
    assert t["via"].startswith("taint/sql")
    tn = _one(ql, "taint request.json sql")
    assert tn["answer"] is False and tn["status"] == "weak_inference" and "not a proof of safety" in tn["why"]
    q = _one(ql, "q 'match (f:function) where f.name = \"unused\" return f' as=bool")
    assert q["answer"] is True and q["at"] == ["app/store.py:20"]
    rows = _one(ql, "q 'match (f:function) where f.file = \"rec.py\" return f' as=rows")
    assert len(rows["answer"]) == 5 and rows["truncated"] is True
    bad = _one(ql, "q 'match (f:fn)' as=bool")
    assert bad["status"] == "invalid" and "unknown kind" in bad["why"]


def test_an_ambiguous_name_is_unknown_with_candidates(tmp_path):
    repo = _scan({"a.py": "def helper():\n    return 1\n\n\ndef use():\n    return helper()\n",
                  "b.py": "def helper():\n    return 2\n"}, tmp_path / "amb")
    a = _one(repo, "exists helper")
    assert a["answer"] is None and a["next"] == "pass path/file.py::symbol"
    assert a["candidates"] == ["helper() a.py:1", "helper() b.py:1"]
    assert _one(repo, "exists a.py::helper")["answer"] is True


def test_a_file_changed_since_the_index_makes_an_answer_unknown(tmp_path):
    repo = _scan(QLANG, tmp_path / "stale")
    web = repo / "app" / "web.py"
    web.write_text(web.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8", newline="\n")
    res = tq.ask(repo, ["calls post_order create_order", "calls create_order save_order", "exists post_order"])
    a, b, c = res["answers"]
    assert a["answer"] is None and a["next"] == "verinoda update"
    assert b["answer"] is True and res["stale_files"] == 1
    assert c["answer"] is None and "app/web.py" in c["why"]
    assert tq.ask(repo, ["exists post_order"], mcp=True)["answers"][0]["next"] == "index_update"


def test_the_test_map_answers_tested_and_never_a_false_no(tmp_path):
    from verinoda import treestate

    repo = _scan(ORDERS, tmp_path / "tm")
    assert _one(repo, "tested tests/test_pricing.py::test_compute_total apply_discount")["why"] == "test never traced"
    fp = {p: treestate.content_id((repo / p).read_bytes()) for p in ("orders/pricing.py", "orders/service.py")}
    db = repo / ".verinoda" / "atlas.db"
    con = sqlite3.connect(db)
    tests = [("tests/test_pricing.py::test_compute_total", 1, "passed"),
             ("tests/test_service.py::test_empty_order_rejected", 1, "failed"),
             ("tests/test_pricing.py::test_no_discount_below_threshold", 0, "passed"),
             ("tests/test_pricing.py::test_discount_applies_above_threshold", 1, "passed")]
    con.executemany("INSERT INTO test_map_tests (test, run_id, commit_sha, complete, outcome, functions, updated_at)"
                    " VALUES (?, 'run_1', 'abcdef1234567890', ?, ?, 1, 't')", tests)
    rows = [("tests/test_pricing.py::test_compute_total", "orders/pricing.py", "compute_total", 0),
            ("tests/test_pricing.py::test_compute_total", "orders/pricing.py", "apply_discount", 0),
            ("tests/test_pricing.py::test_compute_total", "orders/service.py", "validate_items", 0),
            ("tests/test_service.py::test_empty_order_rejected", "orders/service.py", "validate_items", 0),
            ("tests/test_pricing.py::test_no_discount_below_threshold", "orders/pricing.py", "apply_discount", 0),
            ("tests/test_pricing.py::test_discount_applies_above_threshold", "orders/pricing.py", "apply_discount",
             0)]
    con.executemany("INSERT INTO test_map (test, path, qual, fingerprint, run_id, fixture) VALUES (?,?,?,?,'run_1',?)",
                    [(t, p, q, fp[p], fx) for t, p, q, fx in rows])
    con.commit()
    con.close()
    res = tq.ask(repo, ["tested tests/test_pricing.py::test_compute_total apply_discount",
                        "tested tests/test_pricing.py::test_compute_total place_order",
                        "tested tests/test_service.py::test_empty_order_rejected place_order",
                        "tested tests/test_pricing.py::test_no_discount_below_threshold compute_total"])
    yes, no, failed, incomplete = res["answers"]
    assert (yes["answer"], yes["status"]) == (True, "observed") and yes["via"] == "in run run_1 @ commit abcdef123456"
    assert (no["answer"], no["status"]) == (False, "strong_inference") and "run-scoped" in no["why"]
    assert failed["answer"] is None and "did not pass" in failed["why"]
    assert incomplete["answer"] is None and "incomplete" in incomplete["why"]
    # a function that ran in a fixture phase of any test: no "no" for another test
    con = sqlite3.connect(db)
    con.execute("UPDATE test_map SET fixture = 1 WHERE qual = 'compute_total'")
    con.commit()
    con.close()
    shared = _one(repo, "tested tests/test_pricing.py::test_discount_applies_above_threshold compute_total")
    assert shared["answer"] is None and "fixture" in shared["why"]
    # another file the test ran changed since the run: the yes is stale, and so not enough
    p = repo / "orders" / "service.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8", newline="\n")
    st = _one(repo, "tested tests/test_pricing.py::test_compute_total apply_discount")
    assert (st["answer"], st["status"], st["enough"]) == (True, "stale", False)
    assert st["next"].startswith("verinoda observe")
    assert _one(repo, "tested tests/test_pricing.py::test_compute_total place_order")["answer"] is None
    # F's own file changed: the name is not resolved against the old index
    assert _one(repo, "tested tests/test_service.py::test_empty_order_rejected validate_items")["next"] == \
        "verinoda update"


# -- the batch ------------------------------------------------------------------------------------------

def test_a_budget_cut_keeps_the_decided_answers(ql):
    res = tq.ask(ql, ["exists save_order", "reaches post_order save_order", "reaches chain1 leaf"],
                 max_expansions=1)
    a, b, c = res["answers"]
    assert res["complete"] is False and a["answer"] is True
    assert b["answer"] is None and b["cut"] is True and b["next"] == "ask again: q2,q3"


def test_one_bad_question_does_not_fail_the_batch(ql):
    res = tq.ask(ql, ["calls post_order", "exists save_order", {"type": "calls", "a": "x"}, 42,
                      {"type": "exists", "name": "save_order", "id": "mine"}])
    st = [a["status"] for a in res["answers"]]
    assert st == ["invalid", "statically_verified", "invalid", "invalid", "statically_verified"]
    assert res["answers"][4]["id"] == "mine" and res["grammar"].startswith("exists NAME")
    with pytest.raises(tq.BatchError):
        tq.ask(ql, ["exists save_order"] * 21)
    with pytest.raises(tq.BatchError):
        tq.ask(ql, [])


def test_the_line_form(ql):
    assert tq.parse_line('route "POST /orders" save_order depth=3') == {
        "type": "route", "route": "POST /orders", "f": "save_order", "depth": 3}
    assert tq.parse_line("which x in a.py|./b.py") == {"type": "which", "name": "x", "in": ["a.py", "b.py"]}
    for bad in ("calls a b depth=11", "calls a b depth=x", "nope a", "exists", 'q "open', "callers a b",
                "exists a scope=web", "calls a b depth=1 depth=2"):
        with pytest.raises(tq.QuestionError):
            tq.parse_line(bad)
    spec = tq.parse_line("q 'match (f) where f.name = \"x\" return f' as=count")
    assert spec["query"] == 'match (f) where f.name = "x" return f' and tq.parse_line(tq.echo(spec)) == spec


def test_answers_are_byte_identical_across_runs(ql):
    batch = ["calls post_order create_order", "reaches chain1 leaf", "callers leaf", "exists delete_order",
             'route "POST /orders" save_order', "writes put_item items", "taint request.args sql",
             "which save_order in app/store.py|app/web.py", "q 'match (f:function) return count(f)' as=count",
             "calls nothing_like_this leaf"]
    runs = []
    for _ in range(2):
        res = tq.compact(tq.ask(ql, batch))
        res.pop("seconds")
        runs.append(json.dumps(res, sort_keys=False, ensure_ascii=False))
    assert runs[0] == runs[1]


def test_the_command_exit_codes(ql, tmp_path, capsys):
    assert cli.main(["tq", "--repo", str(ql), "calls post_order create_order", "callers leaf"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("tq 2 questions, index fresh,")
    assert "q1 calls post_order create_order = yes | statically_verified | app/web.py:11" in out
    assert "q2 callers leaf = >=2 | strong_inference" in out
    assert cli.main(["tq", "--repo", str(ql), "exists helper_that_is_not_here", "--json"]) == 0
    js = json.loads(capsys.readouterr().out)
    assert js["schema"] == "verinoda.tq/1" and js["answers"][0]["answer"] is False
    assert "question" not in js["answers"][0]
    assert cli.main(["tq", "--repo", str(ql), 'route "DELETE /x" save_order']) == 1
    capsys.readouterr()
    assert cli.main(["tq", "--repo", str(ql), "calls a"]) == 1
    capsys.readouterr()
    assert cli.main(["tq", "--repo", str(ql)]) == 2
    assert cli.main(["tq", "--repo", str(ql), *(["callers leaf"] * 21)]) == 2
    capsys.readouterr()
    f = tmp_path / "qs.txt"
    f.write_text('# questions\nexists save_order\n{"type": "callers", "f": "leaf"}\n', encoding="utf-8")
    assert cli.main(["tq", "--repo", str(ql), "-f", str(f), "--json"]) == 0
    assert [a["answer"] for a in json.loads(capsys.readouterr().out)["answers"]] == [True, 2]
    f.write_text('{"type": \n', encoding="utf-8")
    assert cli.main(["tq", "--repo", str(ql), "-f", str(f)]) == 2
    capsys.readouterr()
    assert cli.main(["tq", "--repo", str(ql), "reaches post_order save_order", "--max-expansions", "1"]) == 3


def test_the_mcp_tool(ql):
    from verinoda.mcp.server import AtlasTools

    t = AtlasTools(ql)
    res = t.tq(["calls post_order create_order", "exists delete_order"])
    assert res["format"] == "text"
    lines = res["text"].splitlines()
    assert lines[1] == "q1 = yes | statically_verified | app/web.py:11"
    assert lines[2].startswith("q2 = no | strong_inference | why: ")
    js = t.tq([{"type": "callers", "f": "leaf"}], format="json")
    assert js["answers"][0]["answer"] == 2 and js["schema"] == "verinoda.tq/1"
    assert t.tq([])["error"] == "invalid_argument"
    assert t.tq(["x"], need="sure")["error"] == "invalid_argument"
