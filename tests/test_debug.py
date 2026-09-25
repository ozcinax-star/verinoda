"""The debug ledger end to end: real isolated runs on git copies of examples/orders_app."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, debug, experiments, treestate  # noqa: E402
from verinoda.claims import Claims  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
PT = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

pytestmark = [pytest.mark.experiment, pytest.mark.skipif(shutil.which("git") is None, reason="git not available")]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, name: str = "oa") -> Path:
    dst = tmp_path / name
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _sub(repo: Path, rel: str, old: str, new: str) -> None:
    p = repo / rel
    t = p.read_text(encoding="utf-8")
    assert old in t
    p.write_bytes(t.replace(old, new, 1).encode("utf-8"))


def _digest(repo: Path) -> dict[str, str]:
    return {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(repo.rglob("*")) if p.is_file() and ".verinoda" not in p.relative_to(repo).parts}


def _rules(res: dict, strength: str = "definitive") -> list[str]:
    return [f["rule"] for f in res["loop"] if f["strength"] == strength]


def test_a_looping_session_is_stopped_and_the_differential_finds_the_cause(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(repo)
    s0 = debug.start(st, repo, "order totals fail after the rename", PT)
    assert s0["outcome"] == "fail" and s0["signature"]["status"] == "parsed"
    assert {f["at_symbol"] for f in s0["signature"]["failures"]} == {"orders/pricing.py::compute_total"}
    assert s0["tree"]["changed_files"] == [{"path": "orders/pricing.py", "status": "modified",
                                            "symbols": ["compute_total"]}]
    assert Path(s0["tree"]["patch"]).read_text(encoding="utf-8").count('i["quantity"]') == 1
    before = _digest(repo)
    _sub(repo, "tests/test_pricing.py", '"qty": 2', '"quantity": 2')
    a1 = debug.attempt(st, repo, hypothesis="the pricing test data still uses the old key")
    assert _rules(a1) == ["test_edited"] and a1["stop"] and a1["progress"] == "improved"
    assert a1["questions_for_human"] and a1["strategies"][0]["id"] == "differential"
    _sub(repo, "orders/pricing.py", 'i["quantity"]', 'i.get("quantity", 0)')
    a2 = debug.attempt(st, repo, hypothesis="a missing quantity should default to zero")
    assert set(_rules(a2, "heuristic")) >= {"error_moved", "masking"} and not a2["stop"]
    _sub(repo, "orders/pricing.py", 'i.get("quantity", 0)', 'i["quantity"]')
    a3 = debug.attempt(st, repo, hypothesis="index quantity directly again")
    assert "tree_reverted" in _rules(a3) and a3["stop"] and a3["tree"]["hash"] == a1["tree"]["hash"]
    d = debug.differential(st, repo)
    assert d["outcome_at_base"] == "pass" and d["hunks"][0]["at"] == "orders/pricing.py:7"
    assert d["hunks"][0]["session_edit"] is False
    status = debug.status(st, repo)
    assert [a["n"] for a in status["attempts"]] == [0, 1, 2, 3, 4] and status["latest"]["stop"]
    after = {k: v for k, v in _digest(repo).items() if k in before}
    changed_by_test = {"tests/test_pricing.py", "orders/pricing.py"}
    assert {k for k in before if before[k] != after.get(k)} <= changed_by_test  # only the test's own edits
    assert "fixed" not in json.dumps(status).lower()


def test_closing_needs_a_passing_attempt_on_the_current_tree(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    st = open_store(repo)
    debug.start(st, repo, "rename", PT)
    with pytest.raises(debug.DebugError):
        debug.close(st, repo, resolved_by=0)  # attempt 0 failed
    _sub(repo, "orders/pricing.py", 'i["quantity"]', 'i["qty"]')
    ok = debug.attempt(st, repo, hypothesis="revert the key")
    assert ok["outcome"] == "pass" and ok["result"].startswith("the repro command passed at tree")
    (repo / "orders" / "extra.py").write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(debug.DebugError, match="changed since attempt 1"):
        debug.close(st, repo, resolved_by=1)
    (repo / "orders" / "extra.py").unlink()
    closed = debug.close(st, repo, resolved_by=1, note="key reverted")
    assert closed["status"] == "resolved" and "passed at tree" in closed["result"]
    with pytest.raises(debug.DebugError):
        debug.attempt(st, repo, closed["session"], hypothesis="more")


def test_agent_reported_runs_are_labelled_and_never_verify(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    log = ("WispTests > wispTicks() FAILED\n    java.lang.IllegalStateException: boom\n"
           "        at com.example.Foo.run(Foo.java:3)\n")
    with pytest.raises(experiments.ExperimentRefused):
        debug.start(st, repo, "gradle fails", ["gradlew", "test"])  # not allowlisted: Verinoda will not run it
    s = debug.start(st, repo, "gradle fails", ["gradlew", "test"], observed_output=log, exit_code=1)
    assert s["run_by"] == "agent" and s["experiment_id"] is None and s["outcome"] == "fail"
    row = st.one("SELECT * FROM debug_attempts WHERE session_id = ?", (s["session"],))
    assert row["output_sha256"] == hashlib.sha256(log.encode()).hexdigest()
    ev = st.evidence(s["evidence_id"])
    assert ev["source_type"] == "agent_report" and ev["meta"]["run_by"] == "agent"
    ok = debug.attempt(st, repo, hypothesis="fixed the null", observed_output="BUILD SUCCESSFUL\n", exit_code=0)
    assert ok["outcome"] == "pass" and any("did not run" in x for x in ok["not_run"])
    from verinoda.claims import ClaimRuleError

    cl = Claims(st, repo)
    c = cl.create("the gradle tests pass", project="oa", snapshot=None, status="unknown")
    cl.attach(c["id"], ok["evidence_id"], "supports")
    with pytest.raises(ClaimRuleError):
        cl.set_status(c["id"], "experiment_verified", reason="the agent reported a passing run")
    assert st.claim(c["id"])["status"] != "experiment_verified"


def test_bisect_finds_the_first_failing_commit(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "tests/test_pricing.py", "def test_compute_total():",
         "def test_threshold_is_not_discounted():\n    assert apply_discount(100.0) == 100.0\n\n\ndef test_compute_total():")
    _git(repo, "commit", "-qam", "c1 threshold test")
    good = _git(repo, "rev-parse", "HEAD").strip()
    _sub(repo, "README.md", "# ", "# Orders: ")
    _git(repo, "commit", "-qam", "c2 readme")
    _sub(repo, "orders/pricing.py", "if subtotal > DISCOUNT_THRESHOLD:", "if subtotal >= DISCOUNT_THRESHOLD:")
    _git(repo, "commit", "-qam", "c3 inclusive threshold")
    bad_commit = _git(repo, "rev-parse", "HEAD").strip()
    _sub(repo, "orders/config.py", "Runtime configuration", "Runtime configuration (ORDERS_*)")
    _git(repo, "commit", "-qam", "c4 config")
    st = open_store(repo)
    s = debug.start(st, repo, "threshold test fails", PT + ["tests/test_pricing.py"])
    assert s["outcome"] == "fail" and not s["tree"]["changed_files"]
    b = debug.bisect(st, repo, good=good)
    assert b["status"] == "found" and b["first_bad_commit"]["commit"] == bad_commit
    assert b["hunks"][0]["at"] == "orders/pricing.py:13" and b["runs_total"] <= 3
    auto = debug.bisect(st, repo)  # no --good: steps back through history
    assert auto["status"] == "found" and auto["first_bad_commit"]["commit"] == bad_commit
    with pytest.raises(ValueError):
        debug.bisect(st, repo, good="--output=x")


def test_trace_enables_off_path(tmp_path):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["price"] * i["qty"]', 'i["price"] + i["qty"]')
    st = open_store(repo)
    debug.start(st, repo, "wrong totals", PT + ["tests/test_pricing.py"], trace=True)
    _sub(repo, "orders/config.py", '        "database_url": DATABASE_URL,',
         '        "currency": "EUR",\n        "database_url": DATABASE_URL,')
    a1 = debug.attempt(st, repo, hypothesis="the settings lack a currency")
    assert _rules(a1) == ["off_path"] and a1["stop"] and a1["trace"]["complete"]
    assert "orders/config.py::load_settings" in a1["loop"][0]["text"]


def test_flaky_sessions_are_flagged_and_rerun_reports_a_pass_rate(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    st = open_store(repo)
    debug.start(st, repo, "sometimes fails", PT + ["tests/test_pricing.py"])  # passes: the example is green
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    # the same tree, reported twice with different outcomes: flaky
    out1 = "FAILED tests/test_pricing.py::test_compute_total - KeyError: 'quantity'\n"
    debug.attempt(st, repo, hypothesis="run it again", kind="probe", observed_output=out1, exit_code=1)
    f = debug.attempt(st, repo, hypothesis="and again", kind="probe", observed_output="3 passed in 0.1s\n",
                      exit_code=0)
    assert f.get("flaky") and f["strategies"][0]["id"] == "rerun" and not f["stop"]
    r = debug.rerun(st, repo, times=3)
    assert r["runs"] == 3 and r["passed"] == 0 and r["conclusion"].startswith("stable")


def test_refs_that_look_like_options_are_refused(tmp_path):
    repo = _repo(tmp_path)
    st = open_store(repo)
    with pytest.raises(ValueError):
        debug.start(st, repo, "x", PT, base="--upload-pack=touch /tmp/p")
    with pytest.raises(ValueError):
        debug.start(st, repo, "x", PT, base="-x")


def test_not_a_git_repository(tmp_path):
    plain = tmp_path / "plain"
    shutil.copytree(EXAMPLE, plain, ignore=shutil.ignore_patterns(".verinoda", "__pycache__"))
    with pytest.raises(treestate.NotAGitTree):
        debug.start(open_store(plain), plain, "x", PT)


# -- CLI ----------------------------------------------------------------------------------------------

def test_cli_debug_commands(tmp_path, capsys):
    repo = _repo(tmp_path)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    r = str(repo)
    assert cli.main(["debug", "start", "totals break", "--repo", r, "--json", "--", *PT]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["attempt"] == 0 and s["session"].startswith("dbg_")
    _sub(repo, "tests/test_pricing.py", '"qty": 2', '"quantity": 2')
    assert cli.main(["debug", "try", "--repo", r, "--hypothesis", "the test data is old"]) == 3  # stop
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("attempt 1 (fix): fail  - STOP: definitive: test_edited")
    assert "ask the user:" in out
    assert cli.main(["debug", "status", "--repo", r]) == 0
    assert "#1 fix" in capsys.readouterr().out
    assert cli.main(["debug", "diff", "--repo", r, "--attempt", "0", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert [f["path"] for f in d["files"]] == ["tests/test_pricing.py"]
    assert cli.main(["debug", "close", "--repo", r, "--abandoned"]) == 0
    assert "abandoned" in capsys.readouterr().out
    assert cli.main(["debug", "start", "gradle", "--repo", r, "--", "gradlew", "test"]) == 3
    assert "refused" in capsys.readouterr().out
    logf = tmp_path / "out.txt"
    logf.write_text("Exception in thread \"main\" java.lang.IllegalStateException: x\n", encoding="utf-8")
    assert cli.main(["debug", "start", "gradle", "--repo", r, "--observed-output", str(logf), "--exit-code", "1",
                     "--json", "--", "gradlew", "test"]) == 0
    assert json.loads(capsys.readouterr().out)["run_by"] == "agent"


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_skills_carry_the_debug_protocol_and_do_not_preapprove_runs(agent):
    from verinoda import agents

    text = agents.render_skill(agent).decode("utf-8")
    for needle in ("verinoda debug start", "debug try --hypothesis", "stop: true", "strategies[0]",
                   "questions_for_human", "Never say \"fixed\"", "--observed-output", "debug_attempt"):
        assert needle in text, needle
    if agent == "claude":
        head = text.split("\n---", 1)[0]  # the front matter (allowed-tools)
        assert "Bash(verinoda debug status *)" in head and "PowerShell(verinoda debug diff *)" in head
        # commands that run the project's tests keep the user's permission prompt
        for cmd in ("start", "try", "differential", "bisect", "rerun", "observe"):
            assert f"verinoda debug {cmd}" not in head, cmd


def test_mcp_debug_tools(tmp_path):
    from verinoda.mcp.server import AtlasTools

    repo = _repo(tmp_path)
    open_store(repo).close()  # an initialised project (.verinoda exists)
    _sub(repo, "orders/pricing.py", 'i["qty"]', 'i["quantity"]')
    t = AtlasTools(repo)
    s = t.debug_start("totals break", PT)
    assert s["attempt"] == 0 and s["outcome"] == "fail"
    a = t.debug_attempt("try again", kind="probe")
    assert a["attempt"] == 1 and a["progress"] == "same"
    st = t.debug_status()
    assert len(st["attempts"]) == 2
    e = t.experiment_run(PT, "the suite fails")
    assert e["outcome"] == "fail" and e["tree"]["hash"] == a["tree"]["hash"]
    refused = t.experiment_run(["gradlew", "test"], "gradle")
    assert refused["status"] == "refused"
    bad = t.debug_strategy("nope")
    assert bad["error"] == "invalid_argument"
