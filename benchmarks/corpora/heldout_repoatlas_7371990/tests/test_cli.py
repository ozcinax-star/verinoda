"""CLI end to end: `python -m repoatlas ...` subprocesses on a git-committed copy of examples/orders_app.

Checks exit codes and the JSON shapes agents rely on. Nothing runs inside examples/.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"
FAKE_SECRET = "sk-ant-FAKE-cli-0000000000"

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(shutil.which("git") is None, reason="git not available")]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _env(**extra: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["GRAPHIFY_OUT"] = ".repoatlas/index"
    env["ANTHROPIC_API_KEY"] = FAKE_SECRET
    env.update(extra)
    return env


def ra(*args: str, cwd: Path, timeout: float = 240, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "repoatlas", *args], cwd=cwd, env=env or _env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, check=False)


def ok_json(*args: str, cwd: Path, rc: int = 0):
    r = ra(*args, cwd=cwd)
    assert r.returncode == rc, (args, r.returncode, r.stdout[-2000:], r.stderr[-2000:])
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    dst = tmp_path_factory.mktemp("cli") / "orders_app"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".repoatlas", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    res = ok_json("init", str(dst), "--json", cwd=dst)
    assert res["config_created"] is True and Path(res["atlas_dir"]) == dst.resolve() / ".repoatlas"
    assert (dst / ".repoatlas" / "config.json").is_file()
    assert ok_json("init", str(dst), "--json", cwd=dst)["config_created"] is False  # idempotent
    scan = ok_json("scan", str(dst), "--json", cwd=dst)
    assert scan["graph"]["nodes"] > 20 and scan["snapshot"]["commit_sha"] and scan["stale"] == []
    from repoatlas.snapshot import list_files

    assert scan["snapshot"]["file_count"] == len(list_files(dst)) and "orders/api.py" in list_files(dst)
    return dst


def test_version_and_help(tmp_path):
    r = ra("--version", cwd=tmp_path)
    assert r.returncode == 0 and r.stdout.startswith("repoatlas ")
    r = ra("--help", cwd=tmp_path)
    assert r.returncode == 0
    for cmd in ("doctor", "init", "scan", "update", "map", "query", "trace", "analyze", "claim", "verify",
                "challenge", "experiment", "memory", "index"):
        assert cmd in r.stdout
    assert ra(cwd=tmp_path).returncode == 0  # no command -> help, exit 0
    assert ra("no-such-command", cwd=tmp_path).returncode == 2


def test_doctor_json_never_prints_secret_values(repo):
    res = ok_json("doctor", "--json", "--repo", str(repo), cwd=repo)
    assert set(res) >= {"ok", "version", "checks", "project", "env", "optional"}
    assert res["ok"] is True
    assert res["env"]["ANTHROPIC_API_KEY"] == "set"
    assert all(v in ("set", "unset") for v in res["env"].values())
    names = {c["check"] for c in res["checks"]}
    assert {"python", "graph", "snapshot", "container_isolation"} <= names
    assert all(set(c) == {"check", "ok", "level", "detail"} for c in res["checks"])
    assert res["project"]["schema_version"] == res["project"]["schema_version_supported"]
    r = ra("doctor", "--repo", str(repo), cwd=repo)
    assert FAKE_SECRET not in r.stdout + r.stderr and "ANTHROPIC_API_KEY=set" in r.stdout


def test_update_is_noop_on_unchanged_tree(repo):
    res = ok_json("update", str(repo), "--json", cwd=repo)
    assert res["mode"] == "noop" and res["changed_count"] == 0 and res["stale"] == []
    r = ra("update", str(repo), cwd=repo)
    assert r.returncode == 0 and r.stdout.startswith("noop")
    if res.get("index_mode"):
        assert f"index {res['index_mode']}" in r.stdout


@pytest.mark.parametrize("view", ["hierarchy", "dependencies", "dataflow", "config", "tests", "history"])
def test_map_views_json(repo, view):
    res = ok_json("map", str(repo), "--view", view, "--json", cwd=repo)
    assert list(res) == [view]
    assert res[view]["coverage"]["method"] and res[view]["coverage"]["limits"]


def test_map_impact_and_human_rendering(repo):
    res = ok_json("map", str(repo), "--view", "impact", "--target", "orders/pricing.py", "--json", cwd=repo)
    assert set(res["impact"]["tests_to_run"]) == {"tests/test_pricing.py", "tests/test_service.py"}
    # No --target: the git working-tree changes are the targets (none here).
    assert ok_json("map", str(repo), "--view", "impact", "--json", cwd=repo)["impact"]["targets"] == []
    r = ra("map", str(repo), "--max-lines", "5", cwd=repo)
    assert r.returncode == 0 and "== hierarchy ==" in r.stdout and "== history ==" in r.stdout
    assert "limit:" in r.stdout and "more lines" in r.stdout
    cfg = ok_json("map", str(repo), "--view", "config", "--json", cwd=repo)["config"]
    assert {"ORDERS_DATABASE_URL", "ORDERS_MAX_ITEMS", "ORDERS_DISCOUNT_THRESHOLD"} <= set(cfg["env_vars"])


def test_map_on_unscanned_repo_fails_cleanly(tmp_path):
    r = ra("map", str(tmp_path), cwd=tmp_path)
    assert r.returncode != 0 and "run `repoatlas scan" in (r.stdout + r.stderr)
    assert "Traceback" not in r.stderr


def test_query_json_and_repo_discovery(repo):
    res = ok_json("query", "where is the discount applied", "--repo", str(repo), "--max-items", "4", "--json",
                  cwd=repo)
    assert set(res) == {"question", "terms", "items", "edges", "budget"}
    assert 0 < len(res["items"]) <= 4 and res["budget"]["used_chars"] <= res["budget"]["max_chars"]
    assert any(i["symbol"] == "apply_discount()" and i["file"] == "orders/pricing.py" for i in res["items"])
    # Without --repo the project is found from the working directory.
    sub = repo / "orders"
    res2 = ok_json("query", "where is the discount applied", "--max-items", "4", "--json", cwd=sub)
    assert [i["id"] for i in res2["items"]] == [i["id"] for i in res["items"]]


def test_trace_found_and_unresolved(repo):
    res = ok_json("trace", "create_order_handler", "OrderRepository.save", "--repo", str(repo), "--json", cwd=repo)
    assert res["status"] == "found"
    hops = res["paths"][0]
    assert [h["to"] for h in hops] == ["place_order()", ".save()"]
    assert hops[1]["derived_by"] == "repoatlas.receiver" and hops[1]["confidence"] == "INFERRED"
    bad = ok_json("trace", "definitely_not_a_symbol_xyz", "place_order", "--repo", str(repo), "--json",
                  cwd=repo, rc=2)
    assert bad["status"] == "unresolved"
    r = ra("trace", "create_order_handler", "OrderRepository.save", "--repo", str(repo), cwd=repo)
    assert r.returncode == 0 and "derived_by=repoatlas.receiver" in r.stdout


def test_analyze_json(repo):
    res = ok_json("analyze", "How does an order get from the API handler to the database?", "--repo", str(repo),
                  "--json", cwd=repo)
    assert set(res) >= {"analysis_id", "question", "intents", "snapshot", "claims", "unknowns", "critique",
                        "usage", "steps"}
    assert res["claims"] and all(set(c) >= {"id", "text", "status", "confidence", "evidence", "uncertainties"}
                                 for c in res["claims"])
    flow = next(c for c in res["claims"] if "place_order() -> .save()" in c["text"])
    assert flow["status"] != "statically_verified"
    assert any(e.startswith("supports:source_code:orders/service.py:22") for e in flow["evidence"])
    budget = ok_json("analyze", "How does an order get from the API handler to the database?", "--repo",
                     str(repo), "--budget-calls", "3", "--json", cwd=repo)
    assert budget["usage"]["exhausted"] == "tool-call budget 3 spent"
    assert any(u["why"] == "tool-call budget 3 spent" for u in budget["unknowns"])
    r = ra("analyze", "Why does OrderRepository use SQLite?", "--repo", str(repo), "--no-challenge", cwd=repo)
    assert r.returncode == 0 and "[primary_source_verified" in r.stdout and "usage:" in r.stdout
    # Critique was switched off, not cut by the budget: the rendering must not claim a reason it lacks.
    assert "[not challenged" in r.stdout and "[not challenged: budget]" not in r.stdout


def test_claim_add_show_list_verify_challenge_memory(repo):
    add = ok_json("claim", "add", "compute_total applies the discount", "--source", "orders/pricing.py:6-8",
                  "--repo", str(repo), "--json", cwd=repo)
    cid = add["id"]
    assert add["status"] == "statically_verified" and add["supporting"][0]["at"] == "orders/pricing.py:6-8"
    assert [h["to_status"] for h in add["history"]] == ["unknown", "statically_verified"]
    no_ev = ok_json("claim", "add", "pricing is fast", "--repo", str(repo), "--json", cwd=repo)
    assert no_ev["status"] == "unknown"  # requested statically_verified, no evidence at all -> unknown

    shown = ok_json("claim", "show", cid, "--repo", str(repo), "--json", cwd=repo)
    assert shown["id"] == cid and set(shown) >= {"supporting", "refuting", "qualifying", "history", "spec"}
    r = ra("claim", "show", cid, "--repo", str(repo), cwd=repo)
    assert r.returncode == 0 and "statically_verified" in r.stdout and "history:" in r.stdout

    lst = ok_json("claim", "list", "--repo", str(repo), "--json", cwd=repo)
    assert {cid, no_ev["id"]} <= {c["id"] for c in lst}
    assert all(set(c) == {"id", "status", "confidence", "kind", "text", "updated_at"} for c in lst)
    only = ok_json("claim", "list", "--status", "unknown", "--repo", str(repo), "--json", cwd=repo)
    assert only and {c["status"] for c in only} == {"unknown"} and no_ev["id"] in {c["id"] for c in only}

    v = ok_json("verify", cid, "--repo", str(repo), "--json", cwd=repo)
    assert v["after"]["status"] == "statically_verified" and v["source_checks"][0]["ok"] is True
    assert v["experiment"] is None

    ch = ok_json("challenge", cid, "--repo", str(repo), "--json", cwd=repo)
    assert ch["claim"] == cid and {"support", "source_recheck", "staleness"} <= {f["check"] for f in ch["findings"]}
    assert ch["after"]["confidence"] <= ch["before"]["confidence"]

    learned = ok_json("memory", "learn", "pricing.entry", "orders/pricing.py::compute_total", "--claim", cid,
                      "--repo", str(repo), cwd=repo)
    assert learned["version"] == 1 and learned["source_claim_id"] == cid and learned["snapshot_id"]
    listed = ok_json("memory", "list", "--repo", str(repo), cwd=repo)
    assert [m["key"] for m in listed] == ["pricing.entry"]
    hist = ok_json("memory", "history", "pricing.entry", "--repo", str(repo), cwd=repo)
    assert len(hist) == 1

    for args in (("claim", "show", "clm_nope"), ("verify", "clm_nope"), ("challenge", "clm_nope")):
        r = ra(*args, "--repo", str(repo), cwd=repo)
        assert r.returncode != 0 and "clm_nope" in (r.stdout + r.stderr) and "Traceback" not in r.stderr, args
    r = ra("claim", "add", "x", "--source", "orders/pricing.py:six", "--repo", str(repo), cwd=repo)
    assert r.returncode != 0 and "must look like" in r.stderr and "Traceback" not in r.stderr
    r = ra("claim", "add", "x", "--source", "orders/missing.py:1", "--repo", str(repo), cwd=repo)
    assert r.returncode != 0 and "does not exist" in r.stderr
    r = ra("memory", "learn", "k", "v", "--claim", "clm_nope", "--repo", str(repo), cwd=repo)
    assert r.returncode != 0 and "no claim clm_nope" in r.stderr and "Traceback" not in r.stderr


def test_experiment_run_allowlisted_and_refused(repo):
    res = ok_json("experiment", "run", "--json", "--hypothesis", "pricing tests pass", "--timeout", "120", "--",
                  "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_pricing.py", cwd=repo)
    assert res["outcome"] == "pass" and res["isolation"] == "process" and res["matches_expectation"] is True
    assert set(res) >= {"id", "guarantees", "summary", "logs", "evidence_id", "duration_s"}
    assert Path(res["logs"]["stdout"]).is_file() and "3 passed" in Path(res["logs"]["stdout"]).read_text(
        encoding="utf-8")
    assert any("3 passed" in s for s in res["summary"]["summary_lines"])
    assert not (repo / ".pytest_cache").exists()
    r = ra("experiment", "run", "--hypothesis", "h", "--", "python", "-m", "pytest", "-q", "-p",
           "no:cacheprovider", "tests/test_pricing.py", cwd=repo)
    assert r.returncode == 0 and "isolation guarantees:" in r.stdout and "NOT: " in r.stdout

    from repoatlas.experiments import container_runtime

    if container_runtime() is None:
        refused = ok_json("experiment", "run", "--json", "--hypothesis", "h", "--", "python", "-c", "print(1)",
                          cwd=repo, rc=3)
        assert refused["status"] == "refused" and "container" in refused["reason"]
    r = ra("experiment", "run", "--hypothesis", "h", cwd=repo)
    assert r.returncode != 0 and "give the command" in (r.stdout + r.stderr)


def test_update_after_edit_marks_claim_stale_via_cli(repo, tmp_path_factory):
    # Work on a separate copy so the module-scoped repo stays unchanged for other tests.
    dst = tmp_path_factory.mktemp("cli_edit") / "orders_app"
    shutil.copytree(repo, dst, ignore=shutil.ignore_patterns(".repoatlas"))
    ok_json("scan", str(dst), "--json", cwd=dst)
    c = ok_json("claim", "add", "compute_total applies the discount", "--source", "orders/pricing.py:6-8",
                "--repo", str(dst), "--json", cwd=dst)
    p = dst / "orders" / "pricing.py"
    original = p.read_bytes()
    p.write_bytes(original.replace(b"apply_discount(subtotal)", b"apply_discount(subtotal) + 0"))
    up = ok_json("update", str(dst), "--json", cwd=dst)
    assert up["mode"] == "incremental" and c["id"] in {s["id"] for s in up["stale"]}
    assert ok_json("claim", "show", c["id"], "--repo", str(dst), "--json", cwd=dst)["status"] == "stale"
    r = ra("update", str(dst), cwd=dst)
    assert r.returncode == 0 and r.stdout.startswith("noop")
    p.write_bytes(original)
    v = ok_json("verify", c["id"], "--repo", str(dst), "--json", cwd=dst)
    assert v["before"]["status"] == "stale" and v["after"]["status"] == "statically_verified"


def test_index_passthrough_to_upstream(repo):
    r = ra("index", "--", "--help", cwd=repo)
    assert r.returncode == 0 and "Usage:" in r.stdout
    assert "path" in r.stdout and "explain" in r.stdout  # upstream subcommands are reachable


# -- `repoatlas index`: upstream installers are blocked ------------------------------------

def _fake_upstream(monkeypatch) -> list:
    """Replace the upstream CLI entry with a recorder, so nothing can reach the real installers."""
    import types

    calls: list = []
    fake = types.ModuleType("repoatlas.project_index.__main__")
    fake.main = lambda: calls.append(list(sys.argv))
    monkeypatch.setitem(sys.modules, "repoatlas.project_index.__main__", fake)
    monkeypatch.setattr(sys, "argv", ["pytest"])
    return calls


def _blocked() -> list[str]:
    from repoatlas import cli

    return sorted(cli._blocked_index_commands())


@pytest.mark.parametrize("name", _blocked())
def test_index_blocks_upstream_installers(name, monkeypatch, capsys):
    from repoatlas import cli

    calls = _fake_upstream(monkeypatch)
    for argv in (["index", "--", name], ["index", "--", name, "install", "--project"], ["index", name, "--help"]):
        assert cli.main(argv) == 2, argv
        err = capsys.readouterr().err
        assert f"`repoatlas index {name}` is blocked" in err and "repoatlas install" in err
    assert calls == [] and sys.argv == ["pytest"]


def test_index_blocklist_matches_upstream_dispatch():
    from repoatlas import cli
    from repoatlas.project_index.install import _CLI_INSTALL_COMMANDS

    # The static list alone covers upstream's install dispatch (every platform alias),
    # plus the git hook and merge-driver writers from upstream cli.py.
    assert set(_CLI_INSTALL_COMMANDS) <= cli.UPSTREAM_BLOCKED
    assert {"install", "uninstall", "hook", "merge-driver", "skills", "claude", "codex"} <= cli.UPSTREAM_BLOCKED
    src = (ROOT / "repoatlas" / "project_index" / "cli.py").read_text(encoding="utf-8")
    assert 'elif cmd == "hook":' in src and 'elif cmd == "merge-driver":' in src
    # Read-only / graph commands stay reachable.
    assert not {"query", "path", "explain", "extract", "update", "export", "hook-check", "hook-guard",
                "god-nodes", "tree", "--help"} & set(_blocked())


def test_index_passes_other_commands_through(monkeypatch):
    from repoatlas import cli

    calls = _fake_upstream(monkeypatch)
    assert cli.main(["index", "--", "explain", "place_order", "--graph", "g.json"]) == 0
    assert cli.main(["index", "path", "a", "b"]) == 0
    assert calls == [["repoatlas index", "explain", "place_order", "--graph", "g.json"],
                     ["repoatlas index", "path", "a", "b"]]


def test_index_install_blocked_end_to_end(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    env = _env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData"),
               XDG_CONFIG_HOME=str(home / ".config"))
    for args in (("index", "--", "install"), ("index", "--", "claude", "install"), ("index", "--", "hook", "install"),
                 ("index", "--", "uninstall", "--purge")):
        r = ra(*args, cwd=work, env=env)
        assert r.returncode == 2 and "repoatlas install" in r.stderr and "Traceback" not in r.stderr, args
    assert list(home.iterdir()) == [] and list(work.iterdir()) == []


# -- exit codes and error handling ----------------------------------------------------

def test_feedback_resolve_rejected_exits_3(repo):
    c = ok_json("claim", "add", "place_order saves the order", "--source", "orders/service.py:22",
                "--repo", str(repo), "--json", cwd=repo)
    fb = ok_json("feedback", "add", "--text", "I doubt place_order saves anything", "--claim", c["id"],
                 "--repo", str(repo), "--json", cwd=repo)
    rej = ok_json("feedback", "resolve", fb["id"], "--verdict", "confirmed", "--reason", "looks right",
                  "--repo", str(repo), "--json", cwd=repo, rc=3)
    assert rej["status"] == "rejected" and "evidence" in rej["error"]
    r = ra("feedback", "resolve", fb["id"], "--verdict", "corrected", "--reason", "x", "--evidence", "evd_nope",
           "--repo", str(repo), cwd=repo)
    assert r.returncode == 3 and "REJECTED" in r.stdout and "Traceback" not in r.stderr
    assert ok_json("feedback", "show", fb["id"], "--repo", str(repo), "--json", cwd=repo)["status"] == "open"
    done = ok_json("feedback", "resolve", fb["id"], "--verdict", "unresolved", "--reason", "cannot tell yet",
                   "--repo", str(repo), "--json", cwd=repo)
    assert done["status"] == "resolved"


def test_value_error_is_reported_not_raised(repo):
    r = ra("feedback", "add", "--text", "   ", "--repo", str(repo), cwd=repo)
    assert r.returncode == 1 and r.stderr.startswith("error: feedback text is empty")
    assert "Traceback" not in r.stderr


@pytest.mark.parametrize("exc", [ValueError("bad value"), re.error("unterminated character set")])
def test_main_turns_value_and_regex_errors_into_exit_1(exc, monkeypatch, capsys):
    from repoatlas import cli

    def boom(args):
        raise exc

    monkeypatch.setattr(cli, "cmd_doctor", boom)
    assert cli.main(["doctor"]) == 1
    assert capsys.readouterr().err.strip() == f"error: {exc}"


def test_claim_add_status_choices(repo):
    from repoatlas import cli
    from repoatlas.claims import ORDER

    assert set(cli.CLAIM_ADD_STATUSES) == set(ORDER) | {"contradicted"} and "stale" not in cli.CLAIM_ADD_STATUSES
    for bad in ("stale", "verified", "nonsense"):
        r = ra("claim", "add", "x", "--status", bad, "--repo", str(repo), cwd=repo)
        assert r.returncode == 2 and "invalid choice" in r.stderr, bad
    c = ok_json("claim", "add", "compute_total never applies the discount", "--source", "orders/pricing.py:6-8",
                "--refutes", "--status", "contradicted", "--repo", str(repo), "--json", cwd=repo)
    assert c["status"] == "contradicted" and c["refuting"]


# -- renderers ------------------------------------------------------------------------

def _analysis(claims: list[dict]) -> dict:
    return {"analysis_id": "ana_x", "snapshot": {"id": "snp_x", "commit": "abc", "dirty": False},
            "intents": ["flow"], "claims": claims, "unknowns": [], "critique": [],
            "usage": {"elapsed_s": 0.1, "tool_calls": 1, "context_tokens_est": 10,
                      "token_count_method": "chars/4 estimate", "exhausted": None}}


def test_analyze_rendering_shows_not_challenged_reason(capsys):
    from repoatlas import cli

    base = {"status": "strong_inference", "confidence": 0.7, "evidence": [], "uncertainties": []}
    cli._r_claims(_analysis([
        {**base, "id": "c1", "text": "challenged"},
        {**base, "id": "c2", "text": "skipped by budget", "challenged": False, "not_challenged_reason": "budget"},
        {**base, "id": "c3", "text": "over the limit", "challenged": False, "not_challenged_reason": "claim_limit"},
        {**base, "id": "c4", "text": "critique off", "challenged": False, "not_challenged_reason": "disabled"},
        {**base, "id": "c5", "text": "older result", "challenged": False},
    ]))
    out = capsys.readouterr().out
    lines = {cid: next(ln for ln in out.splitlines() if f"({cid})" in ln) for cid in ("c1", "c2", "c3", "c4", "c5")}
    assert "not challenged" not in lines["c1"]
    assert lines["c2"].endswith("[not challenged: budget]")
    assert lines["c3"].endswith("[not challenged: claim_limit]")
    assert lines["c4"].endswith("[not challenged: disabled]")
    assert lines["c5"].endswith("[not challenged]")  # no reason given -> none invented


def test_update_rendering_and_error_exit(monkeypatch, capsys, tmp_path):
    from repoatlas import cli, workflow

    cli._r_update({"mode": "incremental", "changed_count": 2, "index_mode": "full", "snapshot": {"id": "snp_1"},
                   "stale": [], "warnings": ["the indexer did not rewrite the graph"]})
    out = capsys.readouterr().out
    assert out.startswith("incremental: 2 changed file(s); index full; snapshot snp_1")
    assert "warning: the indexer did not rewrite the graph" in out

    failed = {"mode": "error", "error": "index build failed: boom", "hint": "run `repoatlas scan --force`"}
    monkeypatch.setattr(cli, "_store", lambda repo, **kw: None)
    monkeypatch.setattr(workflow, "update", lambda st, repo: failed)
    assert cli.main(["update", str(tmp_path)]) == 1
    cap = capsys.readouterr()
    assert cap.out.startswith("error: 0 changed file(s)")
    assert "error: index build failed: boom" in cap.err and "hint: run `repoatlas scan --force`" in cap.err
    assert cli.main(["update", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == failed


def test_research_git_never_reads_stdin(monkeypatch):
    from repoatlas import research

    seen: dict = {}

    def fake_run(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, "git version x", "")

    monkeypatch.setattr(research.subprocess, "run", fake_run)
    assert research._git(None, "--version") == (0, "git version x", "")
    assert seen["stdin"] is subprocess.DEVNULL and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
