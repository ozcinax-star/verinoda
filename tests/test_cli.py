"""CLI end to end: `python -m verinoda ...` subprocesses on a git-committed copy of examples/orders_app.

Checks exit codes and the JSON shapes agents rely on. Nothing runs inside examples/.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

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
    env["GRAPHIFY_OUT"] = ".verinoda/index"
    env["ANTHROPIC_API_KEY"] = FAKE_SECRET
    env.update(extra)
    return env


def ra(*args: str, cwd: Path, timeout: float = 240, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "verinoda", *args], cwd=cwd, env=env or _env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, check=False)


def ok_json(*args: str, cwd: Path, rc: int = 0):
    r = ra(*args, cwd=cwd)
    assert r.returncode == rc, (args, r.returncode, r.stdout[-2000:], r.stderr[-2000:])
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    dst = tmp_path_factory.mktemp("cli") / "orders_app"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    res = ok_json("init", str(dst), "--json", cwd=dst)
    assert res["config_created"] is True and Path(res["atlas_dir"]) == dst.resolve() / ".verinoda"
    assert (dst / ".verinoda" / "config.json").is_file()
    assert ok_json("init", str(dst), "--json", cwd=dst)["config_created"] is False  # idempotent
    scan = ok_json("scan", str(dst), "--json", cwd=dst)
    assert scan["graph"]["nodes"] > 20 and scan["snapshot"]["commit_sha"] and scan["stale"] == []
    from verinoda.snapshot import list_files

    assert scan["snapshot"]["file_count"] == len(list_files(dst)) and "orders/api.py" in list_files(dst)
    return dst


def test_version_and_help(tmp_path):
    r = ra("--version", cwd=tmp_path)
    assert r.returncode == 0 and r.stdout.startswith("verinoda ")
    r = ra("--help", cwd=tmp_path)
    assert r.returncode == 0
    for cmd in ("doctor", "init", "scan", "update", "map", "query", "trace", "analyze", "claim", "verify",
                "challenge", "experiment", "memory", "index", "plan", "resolve", "resolve-call", "observe",
                "research", "benchmark"):
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
    # round-3 state: search index, receiver sidecar, lexicon, precise, tracer, SCIP, reference network
    assert {"search_index", "receiver_calls", "lexicon", "precise", "tracer", "scip", "reference_network",
            "packaging"} <= names
    proj = res["project"]
    si = proj["search_index"]
    assert si["exists"] and si["units"] > 10 and si["generation"] >= 1 and si["versions_match"] is True
    assert si["matches_graph"] is True and si["stale_files"] == 0
    assert proj["receiver_calls"]["matches_graph"] is True and proj["receiver_calls"]["edges"] >= 1
    assert proj["lexicon"]["exists"] and proj["lexicon"]["seed_entries"] > 100 and proj["lexicon"]["built_at"]
    assert proj["references"]["network"] == "cache" and proj["references"]["blocked_hosts"] == {}
    assert proj["tracer"]["sys_monitoring"] is (sys.version_info >= (3, 12))
    assert proj["scip"] == {"index": None}
    r = ra("doctor", "--repo", str(repo), cwd=repo)
    assert FAKE_SECRET not in r.stdout + r.stderr and "ANTHROPIC_API_KEY=set" in r.stdout
    assert "search_index: generation" in r.stdout and "lexicon:" in r.stdout and "research.network = cache" in r.stdout


def test_update_is_noop_on_unchanged_tree(repo):
    res = ok_json("update", str(repo), "--json", cwd=repo)
    assert res["mode"] == "noop" and res["changed_count"] == 0 and res["stale"] == []
    # the same project as --repo (as for query), or found from the working directory
    assert ok_json("update", "--repo", str(repo), "--json", cwd=repo.parent)["mode"] == "noop"
    assert ok_json("update", "--json", cwd=repo / "orders")["mode"] == "noop"
    r = ra("scan", cwd=repo)  # scanning is never implied: a path or --repo is required
    assert r.returncode != 0 and "verinoda scan ." in (r.stderr + r.stdout)
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
    assert r.returncode != 0 and "run `verinoda scan" in (r.stdout + r.stderr)
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


def test_query_prints_model_text_by_default_even_on_a_legacy_code_page(repo):
    # The default is the skeleton-first plain text of retrieval.render_text (docs/DESIGN.md D20).
    # PYTHONIOENCODING=cp1254 (a Turkish Windows console) cannot encode the arrow: stdout must
    # still be UTF-8, not a UnicodeEncodeError half-way through.
    q = "where is the discount applied → compute_total"
    r = subprocess.run([sys.executable, "-m", "verinoda", "query", q, "--repo", str(repo), "--max-chars", "1500"],
                       cwd=repo, env=_env(PYTHONIOENCODING="cp1254"), capture_output=True, timeout=240,
                       stdin=subprocess.DEVNULL, check=False)
    out = r.stdout.decode("utf-8").replace("\r\n", "\n")  # text-mode stdout on Windows
    assert r.returncode == 0 and b"Traceback" not in r.stderr, r.stderr[-2000:]
    # no echo of the question; the passage starts at the signature, so the header names the function
    assert q not in out and "## orders/pricing.py:6-8 compute_total\n" in out
    assert "\ndef compute_total(" in out.split("## orders/pricing.py:6-8 compute_total\n", 1)[1]
    assert "apply_discount" in out and "calls: " in out
    assert len(out) <= 1500 + 200 and not out.lstrip().startswith("{")


def test_write_falls_back_to_utf8_bytes(monkeypatch):
    import io

    from verinoda import cli

    raw = io.BytesIO()
    strict = io.TextIOWrapper(raw, encoding="cp1254", errors="strict")
    monkeypatch.setattr(sys, "stdout", strict)
    cli._write("apply_discount → total ğüşiöç")
    strict.flush()
    assert raw.getvalue().decode("utf-8").endswith("→ total ğüşiöç\n")


def test_trace_found_and_unresolved(repo):
    res = ok_json("trace", "create_order_handler", "OrderRepository.save", "--repo", str(repo), "--json", cwd=repo)
    assert res["status"] == "found"
    hops = res["paths"][0]
    assert [h["to"] for h in hops] == ["place_order()", ".save()"]
    assert hops[1]["derived_by"] == "verinoda.receiver" and hops[1]["confidence"] == "INFERRED"
    bad = ok_json("trace", "definitely_not_a_symbol_xyz", "place_order", "--repo", str(repo), "--json",
                  cwd=repo, rc=2)
    assert bad["status"] == "unresolved"
    r = ra("trace", "create_order_handler", "OrderRepository.save", "--repo", str(repo), cwd=repo)
    assert r.returncode == 0 and "derived_by=verinoda.receiver" in r.stdout
    # an unresolved endpoint comes with hints and the next step; a structural path says so
    r = ra("trace", "OrderService", "orders/repository.py", "--mode", "any", "--repo", str(repo), cwd=repo)
    assert r.returncode == 2 and "'OrderService' did not resolve; did you mean:" in r.stdout
    assert "place_order()  orders/service.py:19" in r.stdout and "\n next: pass one of the hints" in r.stdout
    r = ra("trace", "OrderRepository", "OrderRepository.save", "--mode", "any", "--repo", str(repo), cwd=repo)
    assert r.returncode == 0 and "(containment)" in r.stdout
    assert " reachability: structural" in r.stdout and " note: a path includes containment hops" in r.stdout


def test_trace_rendering_of_hints_and_notes(capsys):
    from verinoda import cli

    cli._r_trace({"source": "a", "target": "zz", "status": "unresolved", "mode": "flow", "paths": [],
                  "hints": {"target": [{"id": "n1", "label": "zap()", "at": "m.py:3"}]},
                  "next_step": "pass one of the hints"})
    cli._r_trace({"source": "a", "target": "b", "status": "no directed path", "mode": "flow", "paths": []})
    cli._r_trace({"source": "a", "target": "b", "status": "found", "mode": "any", "reachability": "execution",
                  "paths": [[{"from": "a()", "to": "b()", "relation": "calls", "kind": "call",
                              "confidence": "EXTRACTED", "at": "m.py:9"}]]})
    out = capsys.readouterr().out
    assert " target 'zz' did not resolve; did you mean:\n   zap()  m.py:3  (id n1)\n next: pass one of the hints" in out
    assert "next: `--mode any` shows structural relations" in out
    assert "a() -calls[EXTRACTED]-> b()  @m.py:9\n reachability: execution" in out and "note:" not in out


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
    add = ok_json("claim", "add", "`compute_total` is defined at orders/pricing.py:6-8", "--source",
                  "orders/pricing.py:6-8", "--kind", "location", "--symbol", "compute_total",
                  "--repo", str(repo), "--json", cwd=repo)
    cid = add["id"]
    assert add["status"] == "statically_verified" and add["supporting"][0]["at"] == "orders/pricing.py:6-8"
    assert [h["to_status"] for h in add["history"]] == ["unknown", "statically_verified"]
    # written text that no typed check covers: its word overlap with the lines is at most weak_inference
    plain = ok_json("claim", "add", "compute_total applies the discount", "--source", "orders/pricing.py:6-8",
                    "--repo", str(repo), "--json", cwd=repo)
    assert plain["status"] == "weak_inference" and "verbatim quote" in plain["history"][-1]["reason"]
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
    # process isolation states its limits (the tests can still write outside the copy)
    assert res["limits"] and "limit: the network is not isolated" in r.stdout

    # a command outside the allowlist: refused without a container runtime, run in a container with one (what
    # the subprocess finds decides: `docker info` can answer differently from one moment to the next on CI)
    r = ra("experiment", "run", "--json", "--hypothesis", "h", "--", "python", "-c", "print(1)", cwd=repo)
    refused = json.loads(r.stdout)
    if r.returncode != 3:
        assert r.returncode == 0 and refused.get("isolation") == "container", (r.returncode, r.stdout[-800:])
    else:
        assert refused["status"] == "refused" and "container" in refused["reason"]
        assert refused["next_step"] and any("allowlist" in lim for lim in refused["limits"])
        # a path outside the repository copy: the refusal names the reason
        r = ra("experiment", "run", "--hypothesis", "h", "--", "python", "-m", "pytest", "-q", "../outside/test_x.py",
               cwd=repo)
        assert r.returncode == 3 and r.stdout.startswith("refused: ") and "limit: " in r.stdout
        assert "next: " in r.stdout and "Traceback" not in r.stderr
    r = ra("experiment", "run", "--hypothesis", "h", cwd=repo)
    assert r.returncode != 0 and "give the command" in (r.stdout + r.stderr)


def test_update_after_edit_marks_claim_stale_via_cli(repo, tmp_path_factory):
    # Work on a separate copy so the module-scoped repo stays unchanged for other tests.
    dst = tmp_path_factory.mktemp("cli_edit") / "orders_app"
    shutil.copytree(repo, dst, ignore=shutil.ignore_patterns(".verinoda"))
    ok_json("scan", str(dst), "--json", cwd=dst)
    c = ok_json("claim", "add", "compute_total calls apply_discount", "--source", "orders/pricing.py:8",
                "--kind", "relation", "--symbol", "apply_discount", "--repo", str(dst), "--json", cwd=dst)
    assert c["status"] == "statically_verified"
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


# -- `verinoda index`: upstream installers are blocked ------------------------------------

def _fake_upstream(monkeypatch) -> list:
    """Replace the upstream CLI entry with a recorder, so nothing can reach the real installers."""
    import types

    calls: list = []
    fake = types.ModuleType("verinoda.project_index.__main__")
    fake.main = lambda: calls.append(list(sys.argv))
    monkeypatch.setitem(sys.modules, "verinoda.project_index.__main__", fake)
    monkeypatch.setattr(sys, "argv", ["pytest"])
    return calls


def _blocked() -> list[str]:
    from verinoda import cli

    return sorted(cli._blocked_index_commands())


@pytest.mark.parametrize("name", _blocked())
def test_index_blocks_upstream_installers(name, monkeypatch, capsys):
    from verinoda import cli

    calls = _fake_upstream(monkeypatch)
    for argv in (["index", "--", name], ["index", "--", name, "install", "--project"], ["index", name, "--help"]):
        assert cli.main(argv) == 2, argv
        err = capsys.readouterr().err
        assert f"`verinoda index {name}` is blocked" in err and "verinoda install" in err
    assert calls == [] and sys.argv == ["pytest"]


def test_index_blocklist_matches_upstream_dispatch():
    from verinoda import cli
    from verinoda.project_index.install import _CLI_INSTALL_COMMANDS

    # The static list alone covers upstream's install dispatch (every platform alias),
    # plus the git hook and merge-driver writers from upstream cli.py.
    assert set(_CLI_INSTALL_COMMANDS) <= cli.UPSTREAM_BLOCKED
    assert {"install", "uninstall", "hook", "merge-driver", "skills", "claude", "codex"} <= cli.UPSTREAM_BLOCKED
    src = (ROOT / "verinoda" / "project_index" / "cli.py").read_text(encoding="utf-8")
    assert 'elif cmd == "hook":' in src and 'elif cmd == "merge-driver":' in src
    # Read-only / graph commands stay reachable.
    assert not {"query", "path", "explain", "extract", "update", "export", "hook-check", "hook-guard",
                "god-nodes", "tree", "--help"} & set(_blocked())


def test_index_passes_other_commands_through(monkeypatch):
    from verinoda import cli

    calls = _fake_upstream(monkeypatch)
    assert cli.main(["index", "--", "explain", "place_order", "--graph", "g.json"]) == 0
    assert cli.main(["index", "path", "a", "b"]) == 0
    assert calls == [["verinoda index", "explain", "place_order", "--graph", "g.json"],
                     ["verinoda index", "path", "a", "b"]]


def test_index_install_blocked_end_to_end(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    env = _env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData"),
               XDG_CONFIG_HOME=str(home / ".config"))
    for args in (("index", "--", "install"), ("index", "--", "claude", "install"), ("index", "--", "hook", "install"),
                 ("index", "--", "uninstall", "--purge")):
        r = ra(*args, cwd=work, env=env)
        assert r.returncode == 2 and "verinoda install" in r.stderr and "Traceback" not in r.stderr, args
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
    from verinoda import cli

    def boom(args):
        raise exc

    monkeypatch.setattr(cli, "cmd_doctor", boom)
    assert cli.main(["doctor"]) == 1
    assert capsys.readouterr().err.strip() == f"error: {exc}"


def test_claim_add_status_choices(repo):
    from verinoda import cli
    from verinoda.claims import ORDER

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
    from verinoda import cli

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
    from verinoda import cli, workflow

    cli._r_update({"mode": "incremental", "changed_count": 2, "index_mode": "full", "snapshot": {"id": "snp_1"},
                   "stale": [], "warnings": ["the indexer did not rewrite the graph"]})
    out = capsys.readouterr().out
    assert out.startswith("incremental: 2 changed file(s); index full; snapshot snp_1")
    assert "warning: the indexer did not rewrite the graph" in out

    failed = {"mode": "error", "error": "index build failed: boom", "hint": "run `verinoda scan --force`"}
    monkeypatch.setattr(cli, "_store", lambda repo, **kw: None)
    monkeypatch.setattr(workflow, "update", lambda st, repo: failed)
    assert cli.main(["update", str(tmp_path)]) == 1
    cap = capsys.readouterr()
    assert cap.out.startswith("error: 0 changed file(s)")
    assert "error: index build failed: boom" in cap.err and "hint: run `verinoda scan --force`" in cap.err
    assert cli.main(["update", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == failed


def test_research_git_never_reads_stdin(monkeypatch):
    from verinoda import research

    seen: dict = {}

    def fake_run(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, "git version x", "")

    monkeypatch.setattr(research.subprocess, "run", fake_run)
    assert research._git(None, "--version") == (0, "git version x", "")
    assert seen["stdin"] is subprocess.DEVNULL and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


# == round 3 wiring: plans, references, runtime, precise, harnesses ==============================

GOLD_APPLY_DISCOUNT = {"tests/test_pricing.py::test_compute_total",
                       "tests/test_pricing.py::test_discount_applies_above_threshold",
                       "tests/test_pricing.py::test_no_discount_below_threshold",
                       "tests/test_service.py::test_place_and_fetch_roundtrip"}
TR_QUESTION = "Sipariş API'den veritabanına nasıl ulaşıyor ve bunu hangi testler kapsıyor?"


def run_cli(capsys, *argv: str, rc: int = 0) -> str:
    """In-process `verinoda ...` (faster than a subprocess); returns stdout."""
    from verinoda import cli

    got = cli.main(list(argv))
    cap = capsys.readouterr()
    assert got == rc, (argv, got, cap.out[-2000:], cap.err[-2000:])
    return cap.out


def _scanned_copy(base: Path) -> Path:
    """A git-committed, scanned copy of orders_app (in-process) for tests that change the tree."""
    from verinoda import workflow
    from verinoda.store import open_store

    dst = _git_copy(base / "orders_app")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst


def _latest_snapshot_id(repo: Path) -> str:
    from verinoda.store import open_store

    st = open_store(repo)
    try:
        return st.latest_snapshot()["id"]
    finally:
        st.close()


# -- config -------------------------------------------------------------------------------------

def test_default_config_round3_values_match_their_modules(tmp_path):
    from verinoda import cli, paths, precise, question_plan, scip_reader, workflow
    from verinoda.runtime import trace as rt

    cfg = paths.DEFAULT_CONFIG
    assert cfg["budget"]["precise_sites"] == precise.DEFAULT_SITES == 20
    assert cfg["budget"]["precise_seconds"] == precise.DEFAULT_SECONDS == 1.5
    assert cfg["research"] == {"network": "cache"}
    assert cfg["understanding"] == question_plan.THRESHOLDS
    assert cli.TRACE_MODES == rt.MODES and cli.NETWORK_CHOICES == paths.NETWORK_MODES
    b = precise.Budget.from_config(tmp_path)
    assert (b.max_sites, b.max_seconds) == (20, 1.5) and paths.network_mode(tmp_path) == "cache"
    # helpers: the places the modules read
    root = tmp_path.resolve()
    assert paths.plans_dir(tmp_path) == root / ".verinoda" / "plans"
    assert paths.http_cache_dir(tmp_path) == paths.research_dir(tmp_path) / "http-cache"
    assert paths.scip_index_path(tmp_path).relative_to(root).as_posix() in scip_reader.CANDIDATES
    # `init` writes the new sections; user overrides merge key by key
    workflow.init(tmp_path)
    written = json.loads((tmp_path / ".verinoda" / "config.json").read_text(encoding="utf-8"))
    assert written["research"] == {"network": "cache"} and written["understanding"]["link"] == 0.70
    assert written["budget"]["precise_sites"] == 20
    (tmp_path / ".verinoda" / "config.json").write_bytes(
        json.dumps({"research": {"network": "off"}, "budget": {"precise_sites": 5}}).encode("utf-8"))
    loaded = paths.load_config(tmp_path)
    assert loaded["budget"]["precise_sites"] == 5 and loaded["budget"]["seconds"] == 60
    assert loaded["budget"]["precise_seconds"] == 1.5 and paths.network_mode(tmp_path) == "off"
    assert precise.Budget.from_config(tmp_path).max_sites == 5
    (tmp_path / ".verinoda" / "config.json").write_bytes(b'{"research": {"network": "sometimes"}}')
    assert paths.network_mode(tmp_path) == "cache"  # an unknown mode never turns the network on


# -- question plans ----------------------------------------------------------------------------------

def test_plan_draft_check_analyze_and_audit(repo, capsys):
    schema = json.loads(run_cli(capsys, "plan", "schema"))
    assert {"schema", "user_message", "sub_questions", "mentions"} <= set(schema["properties"])
    d = json.loads(run_cli(capsys, "plan", "draft", TR_QUESTION, "--repo", str(repo), "--json"))
    path = Path(d["file"]) if Path(d["file"]).is_absolute() else Path.cwd() / d["file"]
    assert path.parent == (repo / ".verinoda" / "plans").resolve() and re.fullmatch(r"plan-\d{3}\.json", path.name)
    raw = path.read_bytes()
    assert b"\r" not in raw and json.loads(raw.decode("utf-8")) == d["plan"]
    assert d["plan"]["user_message"] == TR_QUESTION and d["plan"]["language"] == "tr"
    assert [sq["intent"] for sq in d["plan"]["sub_questions"]] == ["flow", "tests"]
    # a bare name is looked up under .verinoda/plans/
    chk = json.loads(run_cli(capsys, "plan", "check", path.name, "--repo", str(repo), "--json"))
    assert chk["status"] == "ready" and chk["plan_id"].startswith("qpl_") and not chk["errors"]
    assert {lk["mention"] for lk in chk["links"]} == {m["id"] for m in d["plan"]["mentions"]}
    an = json.loads(run_cli(capsys, "analyze", "--plan", str(path), "--no-challenge", "--repo", str(repo), "--json"))
    assert an["status"] == "answered" and an["plan_source"] == "host" and an["question"] == TR_QUESTION
    assert an["understood_as"] and an["plan_check"]["status"] == "ready"
    assert [s["id"] for s in an["subquestions"]] == ["q1", "q2"] and all(s["status"] for s in an["subquestions"])
    audit = json.loads(run_cli(capsys, "plan", "audit", an["analysis_id"], "--no-refresh", "--repo", str(repo),
                               "--json"))
    assert audit["analysis_id"] == an["analysis_id"] and audit["refreshed"] is None
    assert [s["id"] for s in audit["subquestions"]] == ["q1", "q2"] and audit["changed"] == []
    assert [s["was"] for s in audit["subquestions"]] == [s["status"] for s in an["subquestions"]]
    out = run_cli(capsys, "plan", "audit", an["analysis_id"], "--no-refresh", "--repo", str(repo))
    assert "changed: none" in out and "  q1 [" in out
    # the human rendering: understood as, one line per sub-question verdict
    r = ra("analyze", "--plan", str(path), "--no-challenge", "--repo", str(repo), cwd=repo)
    assert r.returncode == 0 and "understood as: " in r.stdout and "\nsub-questions:\n  q1 [" in r.stdout
    assert "Sipariş" in r.stdout


def test_plan_exit_codes_clarification_invalid_and_no_inline_json(repo, capsys):
    plans = repo / ".verinoda" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    clar = {"schema": "verinoda.question_plan/1", "user_message": "Eski sürümde indirim nasıl hesaplanıyordu?",
            "language": "tr", "restated_goal": "how the discount was computed in an older version",
            "sub_questions": [{"id": "q1", "text": "how was the discount computed", "intent": "flow",
                               "done_when": {"kind": "path_found", "detail": "x"}}], "on_ambiguity": "ask"}
    (plans / "clar.json").write_bytes(json.dumps(clar, ensure_ascii=False).encode("utf-8"))
    chk = json.loads(run_cli(capsys, "plan", "check", "clar.json", "--repo", str(repo), "--json", rc=3))
    assert chk["status"] == "needs_clarification" and chk["clarifications"][0]["id"] == "c-version"
    an = json.loads(run_cli(capsys, "analyze", "--plan", "clar.json", "--repo", str(repo), "--json", rc=3))
    assert an["status"] == "needs_clarification" and an["claims"] == [] and an["clarifications"]
    out = run_cli(capsys, "analyze", "--plan", "clar.json", "--repo", str(repo), rc=3)
    assert "clarification needed" in out and "c-version: 'Eski sürümde'" in out and "__other__" in out

    (plans / "bad.json").write_bytes(b'{"schema": "verinoda.question_plan/1", "user_message": "x"}')
    bad = json.loads(run_cli(capsys, "plan", "check", "bad.json", "--repo", str(repo), "--json", rc=2))
    assert bad["status"] == "invalid" and {"/language", "/sub_questions"} <= {e["at"] for e in bad["errors"]}
    an = json.loads(run_cli(capsys, "analyze", "--plan", "bad.json", "--repo", str(repo), "--json", rc=2))
    assert an["status"] == "invalid_plan" and an["errors"] and an["claims"] == []
    (plans / "broken.json").write_bytes(b"{not json")
    broken = json.loads(run_cli(capsys, "plan", "check", "broken.json", "--repo", str(repo), "--json", rc=2))
    assert broken["status"] == "invalid" and broken["errors"][0]["code"] == "schema"

    for args in (("analyze", "--plan", '{"schema": 1}'), ("plan", "check", '{"schema": 1}')):
        r = ra(*args, "--repo", str(repo), cwd=repo)
        assert r.returncode == 1 and "never as JSON on the command line" in r.stderr, args
        assert "Traceback" not in r.stderr
    r = ra("plan", "check", "missing.json", "--repo", str(repo), cwd=repo)
    assert r.returncode == 1 and "plan file missing.json not found" in r.stderr
    r = ra("analyze", "--repo", str(repo), cwd=repo)
    assert r.returncode == 1 and "give a question" in r.stderr


def test_plan_draft_out_and_check_rendering(repo, capsys):
    from verinoda import cli

    d = json.loads(run_cli(capsys, "plan", "draft", "where is the discount applied?", "--out", "mine.json",
                           "--repo", str(repo), "--json"))
    assert (repo / ".verinoda" / "plans" / "mine.json").is_file()  # a bare name lands under plans/
    assert Path(d["file"]).name == "mine.json"
    with pytest.raises(SystemExit, match="must be a .json file"):
        cli.main(["plan", "draft", "x", "--out", "mine.txt", "--repo", str(repo)])
    cli._r_plan_check({
        "status": "needs_clarification", "plan_id": "qpl_1", "file": ".verinoda/plans/p.json", "errors": [],
        "warnings": [{"code": "relative_version", "msg": "the user named a relative version"}],
        "links": [{"mention": "m1", "text": "indirim", "status": "linked", "at": "orders/pricing.py:11-15",
                   "label": "apply_discount()", "score": 0.75, "tier": "seed_dictionary"},
                  {"mention": "m2", "text": "foo", "status": "unlinked", "rejected": ["Foo.bar"]}],
        "references": [{"reference": "r1", "text": "v1.2", "version_used": "v1.2", "status": "resolved",
                        "evidence": "local git tag v1.2"},
                       {"reference": "r2", "text": "eski", "status": "unresolved", "next_step": "ask which version"}],
        "clarifications": [{"id": "c-r2", "question_user_lang": "Hangi sürüm?", "options": [
            {"value": "v1.2", "label": "v1.2", "evidence": "git tag"}]}],
        "unknowns": [{"question": "what is 'foo'?", "why": "no match", "next_step": "give the identifier"}]})
    out = capsys.readouterr().out
    assert out.startswith("plan check: needs_clarification  (plan qpl_1 from .verinoda/plans/p.json)")
    assert "m1 'indirim': linked apply_discount() at orders/pricing.py:11-15 (seed_dictionary, 0.75)" in out
    assert "m2 'foo': unlinked; rejected candidates: Foo.bar" in out
    assert "reference r1 'v1.2' @ v1.2: resolved (local git tag v1.2)" in out
    assert "reference r2 'eski': unresolved; next: ask which version" in out
    assert "c-r2: Hangi sürüm?\n       - v1.2: v1.2  [git tag]" in out and "unknown: what is 'foo'?" in out
    assert "next: verinoda analyze" not in out  # only a ready plan is handed to analyze


def test_analyze_passes_observe_and_maps_statuses_to_exit_codes(repo, capsys, monkeypatch):
    from verinoda import analysis

    seen: dict = {}
    current = {"status": "answered"}

    def fake(store, repo_, question, **kw):
        seen.update(kw, question=question)
        return {"analysis_id": "ana_x", "status": current["status"], "question": question, "intents": ["flow"],
                "snapshot": None, "claims": [], "unknowns": [], "critique": []}

    monkeypatch.setattr(analysis, "analyze", fake)
    for status, rc in (("answered", 0), ("invalid_plan", 2), ("needs_clarification", 3)):
        current["status"] = status
        run_cli(capsys, "analyze", "how is the discount applied?", "--observe", "--repo", str(repo), "--json", rc=rc)
    assert seen["observe"] is True and seen["plan"] is None and seen["run_tests"] is False
    current["status"] = "answered"
    run_cli(capsys, "analyze", "q", "--repo", str(repo), "--json")
    assert seen["observe"] is False and seen["question"] == "q"


# -- references and research ----------------------------------------------------------------------

def test_resolve_offline_text_json_and_exit_codes(repo, capsys):
    out = run_cli(capsys, "resolve", "orders/pricing.py'deki apply_discount nasıl çalışıyor?", "--network", "off",
                  "--repo", str(repo))
    assert out.startswith("References (complete): 1 pinned") and "r1 local_file orders/pricing.py" in out
    text = ("requests 2.31 sürümünde https://github.com/psf/requests/blob/main/src/requests/sessions.py#L100-L120 "
            "nasıl çalışıyor?")
    res = json.loads(run_cli(capsys, "resolve", text, "--network", "off", "--repo", str(repo), "--json", rc=3))
    assert set(res) == {"id", "status", "references", "unbound", "questions_for_user", "summary"}  # compact
    assert res["status"] != "complete" and res["id"].startswith("rrs_")
    ghf = next(r for r in res["references"] if r["class"] == "git_file")
    assert ghf["lines"] == [100, 120] and ghf["unresolved"][0]["next_step"]
    assert res["summary"]["silent_floating_pins"] == 0


def test_resolve_passes_references_network_and_local_intent(repo, tmp_path, capsys, monkeypatch):
    from verinoda import references

    seen: dict = {}

    def fake(store, repo_, text, **kw):
        seen.update(kw, text=text, repo=repo_)
        return {"id": "rrs_x", "status": "partial", "references": [], "unbound_mentions": [],
                "questions_for_user": [{"id": "q1", "question": "which one?"}], "summary": {}}

    monkeypatch.setattr(references, "resolve", fake)
    res = json.loads(run_cli(capsys, "resolve", "see a and b", "--reference", "https://github.com/a/b@v1",
                             "--reference", "c/d", "--local-intent", "--repo", str(repo), "--json", rc=3))
    assert res["questions_for_user"] and seen["text"] == "see a and b"
    assert seen["explicit"] == ["https://github.com/a/b@v1", "c/d"] and seen["network"] == "cache"
    assert seen["local_intent"] is True
    run_cli(capsys, "resolve", "x", "--network", "on", "--repo", str(repo), "--json", rc=3)
    assert seen["network"] == "on" and seen["local_intent"] is None and seen["explicit"] == []
    # the default comes from config research.network
    (tmp_path / ".verinoda").mkdir()
    (tmp_path / ".verinoda" / "config.json").write_bytes(b'{"research": {"network": "off"}}')
    run_cli(capsys, "resolve", "x", "--repo", str(tmp_path), "--json", rc=3)
    assert seen["network"] == "off" and Path(seen["repo"]) == tmp_path.resolve()


def _reference_repo(base: Path) -> tuple[Path, str]:
    ref = base / "refrepo"
    ref.mkdir()
    body = (b"def fetch(url):\n    for attempt in range(3):\n        try:\n            return get(url)\n"
            b"        except IOError:\n            continue\n")
    (ref / "client.py").write_bytes(body)
    _git(ref, "init", "-q")
    _git(ref, "add", "-A")
    _git(ref, "commit", "-q", "-m", "v1")
    _git(ref, "tag", "v1")
    v1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ref, capture_output=True, text=True,
                        check=True).stdout.strip()
    (ref / "client.py").write_bytes(body + b"# v2\n")
    _git(ref, "commit", "-q", "-am", "v2")
    return ref, v1


def test_research_a_reference_pinned_by_resolve(tmp_path, capsys):
    ref, v1 = _reference_repo(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    res = json.loads(run_cli(capsys, "resolve", "how does fetch retry work in the reference?", "--reference",
                             f"{ref.as_posix()}@v1", "--network", "off", "--repo", str(proj), "--json"))
    (r1,) = res["references"]
    assert r1["status"] == "pinned" and r1["value"] == v1  # the named tag, not the newer branch head
    out = json.loads(run_cli(capsys, "research", "--resolution", res["id"], "--reference-id", "r1", "--topic",
                             "fetch retry", "--repo", str(proj), "--json"))
    assert out["status"] == "ok" and out["resolved_commit"] == v1
    text = run_cli(capsys, "research", "--resolution", res["id"], "--reference-id", "r1", "--repo", str(proj))
    assert text.startswith("research ") and v1 in text

    # an unresolved reference: unknown + next step, nothing researched
    bad = json.loads(run_cli(capsys, "resolve", "https://github.com/psf/requests/blob/main/src/x.py", "--network",
                             "off", "--repo", str(proj), "--json", rc=3))
    un = json.loads(run_cli(capsys, "research", "--resolution", bad["id"], "--reference-id", "r1", "--repo",
                            str(proj), "--json", rc=3))
    assert un["status"] == "unresolved" and un["next_step"] and un["why"]
    from verinoda import cli

    assert cli.main(["research", "--resolution", res["id"], "--reference-id", "r9", "--repo", str(proj)]) == 1
    assert "has no reference r9 (it has: r1)" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="go together"):
        cli.main(["research", "--resolution", res["id"], "--repo", str(proj)])
    with pytest.raises(SystemExit, match="give a reference"):
        cli.main(["research", "--repo", str(proj)])


def test_research_no_verify_source_is_passed_through(tmp_path, capsys, monkeypatch):
    from verinoda import research

    seen: list = []
    monkeypatch.setattr(research, "research_full", lambda st, repo_, reference, **kw: seen.append((reference, kw))
                        or {"status": "ok"})
    monkeypatch.setattr(research, "compact", lambda full: {"status": full["status"]})
    run_cli(capsys, "research", "pkg:pypi/requests@2.31.0", "--no-verify-source", "--repo", str(tmp_path), "--json")
    run_cli(capsys, "research", "https://github.com/a/b", "--ref", "v1", "--repo", str(tmp_path), "--json")
    assert seen[0][0] == "pkg:pypi/requests@2.31.0" and seen[0][1]["verify_source"] is False
    assert seen[1][1]["verify_source"] is True and seen[1][1]["ref"] == "v1"


def test_feedback_repeatable_references_and_network(repo, capsys, monkeypatch):
    from verinoda import feedback

    calls: list = []
    monkeypatch.setattr(feedback, "add", lambda st, repo_, text, **kw: calls.append(("add", kw)) or {"id": "fbk_x"})
    monkeypatch.setattr(feedback, "process", lambda st, repo_, fid, **kw: calls.append(("process", fid, kw))
                        or {"id": fid, "status": "processed"})
    run_cli(capsys, "feedback", "add", "--text", "t", "--reference", "https://github.com/a/b", "--ref", "v1",
            "--reference", "c/d@v2", "--reference", "pkg==1.0", "--process", "--network", "off",
            "--repo", str(repo), "--json")
    assert calls[0][1]["reference"] == "https://github.com/a/b" and calls[0][1]["ref"] == "v1"
    assert calls[0][1]["references"] == ["c/d@v2", "pkg==1.0"]
    assert calls[1] == ("process", "fbk_x", {"topic": None, "network": "off"})
    run_cli(capsys, "feedback", "process", "fbk_y", "--network", "on", "--repo", str(repo), "--json")
    assert calls[2] == ("process", "fbk_y", {"topic": None, "network": "on"})
    run_cli(capsys, "feedback", "add", "--text", "t2", "--repo", str(repo), "--json")
    assert calls[3][1]["reference"] is None and calls[3][1]["references"] is None


# -- claims bound to the current tree, graded by kind (audit criterion 7) ----------------------------

def test_claim_add_binds_to_the_current_tree_and_grades_by_kind(tmp_path, capsys, monkeypatch):
    from verinoda.claims import VERIFIED

    dst = _scanned_copy(tmp_path)
    scanned = _latest_snapshot_id(dst)
    p = dst / "orders" / "pricing.py"
    p.write_bytes(p.read_bytes().replace(b'"""Pricing rules."""\n', b'"""Pricing rules."""\n\n# two lines down\n'))
    loc = json.loads(run_cli(capsys, "claim", "add", "apply_discount is defined here", "--source",
                             "orders/pricing.py:13-17", "--kind", "location", "--symbol", "apply_discount",
                             "--repo", str(dst), "--json"))
    # bound to a snapshot of the edited tree, not to the scan's (which describes other line numbers)
    assert loc["snapshot_id"] == _latest_snapshot_id(dst) != scanned
    assert loc["kind"] == "location" and loc["spec"]["symbol"] == "apply_discount"
    assert loc["status"] == "statically_verified" and loc["supporting"][0]["grade"] == "full"
    wrong = json.loads(run_cli(capsys, "claim", "add", "compute_total is defined here", "--source",
                               "orders/pricing.py:13-17", "--kind", "location", "--symbol", "compute_total",
                               "--repo", str(dst), "--json"))
    assert wrong["status"] not in VERIFIED and wrong["supporting"][0]["grade"] != "full"
    rel = json.loads(run_cli(capsys, "claim", "add", "compute_total calls apply_discount", "--source",
                             "orders/pricing.py:10", "--kind", "relation", "--symbol", "apply_discount",
                             "--repo", str(dst), "--json"))
    assert rel["status"] == "statically_verified" and rel["spec"]["target_label"] == "apply_discount"
    assert rel["spec"]["at"] == "orders/pricing.py:10" and rel["snapshot_id"] == loc["snapshot_id"]
    norel = json.loads(run_cli(capsys, "claim", "add", "compute_total calls validate_items", "--source",
                               "orders/pricing.py:10", "--kind", "relation", "--symbol", "validate_items",
                               "--repo", str(dst), "--json"))
    assert norel["status"] not in VERIFIED and norel["supporting"][0]["grade"] == "none"
    cfg = json.loads(run_cli(capsys, "claim", "add", "the database URL comes from ORDERS_DATABASE_URL", "--source",
                             "orders/config.py:5-7", "--kind", "config", "--symbol", "ORDERS_DATABASE_URL",
                             "--repo", str(dst), "--json"))
    assert cfg["status"] == "statically_verified" and cfg["spec"]["env"] == "ORDERS_DATABASE_URL"
    # a refused index refresh: no claim bound to a snapshot of another tree
    from verinoda import workflow

    monkeypatch.setattr(workflow, "_current_snapshot",
                        lambda st, repo_: ({"id": "snp_old"}, {"error": "the indexer did not rewrite the graph",
                                                               "hint": "verinoda scan --force"}))
    with pytest.raises(SystemExit, match="could not be refreshed"):
        run_cli(capsys, "claim", "add", "x", "--source", "orders/pricing.py:13", "--repo", str(dst))
    r = ra("claim", "add", "x", "--kind", "nonsense", "--repo", str(dst), cwd=dst)
    assert r.returncode == 2 and "invalid choice" in r.stderr


def test_claim_add_binds_roles_and_contradicts_definitive_misses_at_creation(repo):
    from verinoda.claims import VERIFIED

    def add(text, *args):
        return ok_json("claim", "add", text, *args, "--repo", str(repo), "--json", cwd=repo)

    # the callee comes from the text when --symbol is not given; the caller is the name before the verb
    rel = add("validate_items is called by place_order", "--source", "orders/service.py:20", "--kind", "relation")
    assert rel["status"] == "statically_verified" and rel["spec"]["target_label"] == "validate_items"
    miss = add("create_order_handler calls save", "--source", "orders/api.py:18", "--kind", "relation",
               "--symbol", "save")
    assert miss["status"] == "contradicted" and miss["refuting"][0]["at"] == "orders/api.py:16-21"
    assert "no direct call to save in create_order_handler" in miss["history"][-1]["reason"]
    cfg = add("The discount threshold is read from ORDERS_MAX_ITEMS", "--source", "orders/config.py:6",
              "--kind", "config", "--symbol", "ORDERS_MAX_ITEMS")
    assert cfg["status"] == "contradicted" and "binds ORDERS_MAX_ITEMS to MAX_ITEMS_PER_ORDER" in \
        cfg["history"][-1]["reason"]
    order = add("`place_order` calls `validate_items` before `save`", "--source", "orders/service.py:19-22",
                "--kind", "order", "--symbol", "place_order")
    assert order["status"] == "statically_verified" and order["kind"] == "behaviour"
    assert order["spec"]["proposition"] == "validate_items before save in place_order"
    wrong = add("`place_order` calls `save` before `validate_items`", "--source", "orders/service.py:19-22",
                "--kind", "order", "--symbol", "place_order")
    assert wrong["status"] == "contradicted"
    general = add("apply_discount returns the subtotal above the threshold", "--source", "orders/pricing.py:11-15")
    assert general["status"] == "weak_inference" and general["status"] not in VERIFIED
    r = ra("claim", "add", "place_order saves before it validates", "--source", "orders/service.py:19-22",
           "--kind", "order", "--symbol", "place_order", "--repo", str(repo), cwd=repo)
    assert r.returncode != 0 and "states two calls" in r.stderr and "Traceback" not in r.stderr
    # a negated order is not read (a reversed proposition would contradict a true sentence)
    r = ra("claim", "add", "`place_order` never calls `save` before `validate_items`", "--source",
           "orders/service.py:19-22", "--kind", "order", "--symbol", "place_order", "--repo", str(repo), cwd=repo)
    assert r.returncode != 0 and "a negated order is not read" in r.stderr
    # "after that" keeps the order as written
    after = add("`place_order` calls `validate_items`, and after that `save`", "--source", "orders/service.py:19-22",
                "--kind", "order", "--symbol", "place_order")
    assert after["status"] == "statically_verified"
    # no callee the text states clearly and no --symbol: asked for, not guessed
    r = ra("claim", "add", "place_order is what create_order_handler calls", "--source", "orders/api.py:18",
           "--kind", "relation", "--repo", str(repo), cwd=repo)
    assert r.returncode != 0 and "--kind relation needs the callee" in r.stderr
    tr = add("validate_items'ı place_order çağırır", "--source", "orders/service.py:20", "--kind", "relation")
    assert tr["status"] == "statically_verified" and tr["spec"]["target_label"] == "validate_items"
    # a module constant is defined by its assignment; a negated sentence is not verified by a positive check
    const = add("`DISCOUNT_THRESHOLD` is defined in orders/config.py", "--source", "orders/config.py:7",
                "--kind", "location", "--symbol", "DISCOUNT_THRESHOLD")
    assert const["status"] == "statically_verified"
    neg = add("place_order does not call validate_items", "--source", "orders/service.py:20", "--kind", "relation",
              "--symbol", "validate_items")
    assert neg["status"] not in VERIFIED and neg["status"] != "contradicted"


def test_order_claim_without_symbol_reads_the_function_from_its_role(repo):
    # review round 2: without --symbol the first name was taken as the function, so these true sentences
    # were checked in validate_items' body and contradicted at creation
    def add(text):
        return ok_json("claim", "add", text, "--source", "orders/service.py:19-22", "--kind", "order", "--repo",
                       str(repo), "--json", cwd=repo)

    for text in ("`validate_items` runs before `save` in `place_order`",
                 "`validate_items` is called before `save` by `place_order`",
                 "`validate_items`, `place_order` içinde `save`'den önce çağrılır",
                 "`save` runs after `validate_items` in `place_order`"):
        c = add(text)
        assert c["status"] == "statically_verified", text
        assert c["spec"]["proposition"] == "validate_items before save in place_order", text
    # the function is not stated: refused, not guessed
    r = ra("claim", "add", "`validate_items` runs before `save`", "--source", "orders/service.py:19-22", "--kind",
           "order", "--repo", str(repo), cwd=repo)
    assert r.returncode != 0 and "without --symbol the text must name it" in r.stderr
    # a function read from the text that the cited lines are not in: refused
    r = ra("claim", "add", "`validate_items` runs before `save` in `compute_total`", "--source",
           "orders/service.py:19-22", "--kind", "order", "--repo", str(repo), cwd=repo)
    assert r.returncode != 0 and "no definition `compute_total` is around orders/service.py:19-22" in r.stderr


def test_names_written_as_code_are_never_replaced_by_similar_ones(repo):
    res = ok_json("analyze", "Where is place_orders defined?", "--repo", str(repo), "--json", cwd=repo)
    (sq,) = res["subquestions"]
    assert sq["status"] == "unmet" and sq["flags"]["not_found"]
    assert res["unknowns"][0]["why"] == ("no symbol named `place_orders` in this repository; nearest: place_order "
                                         "(orders/service.py:19)")
    link = res["plan_check"]["links"][0]
    assert link["status"] == "not_found" and link["did_you_mean"] == ["place_order (orders/service.py:19)"]
    assert not any("place_order()" in c["text"] for c in res["claims"])  # nothing about the similar name
    r = ra("trace", "create_order_handler", "place_orders", "--repo", str(repo), cwd=repo)
    assert r.returncode == 2 and "[unresolved" in r.stdout
    assert "target: no symbol named `place_orders` in this repository; nearest: place_order " \
           "(orders/service.py:19)" in r.stdout
    r = ra("trace", "create order handler", "OrderRepository.save", "--repo", str(repo), cwd=repo)
    assert r.returncode == 0 and "source: 'create order handler' has no exact match; resolved by similarity to " \
                                 "create_order_handler()" in r.stdout


def test_verify_rendering_shows_status_details_note_and_unconfirmed(capsys):
    from verinoda import cli

    cli._r_verify({
        "claim": "clm_1", "before": {"status": "statically_verified", "confidence": 0.9},
        "after": {"status": "stale", "confidence": 0.4},
        "source_checks": [{"ok": True, "at": "a.py:3", "reason": "moved", "status": "moved", "moved_to": [5, 5],
                           "candidate": None},
                          {"ok": False, "at": "b.py:9", "reason": "changed", "status": "changed", "moved_to": None,
                           "candidate": [12, 13]}],
        "experiment": None, "unconfirmed_files": ["c.py"], "note": "verify: c.py changed since the claim's snapshot",
        "index_refresh": {"mode": "incremental", "stale": ["clm_1"], "error": "refused", "hint": "scan --force"}})
    out = capsys.readouterr().out
    assert "clm_1: statically_verified -> stale (0.40)" in out
    assert "ok  a.py:3: moved  [status moved; moved to 5-5]" in out
    assert "BAD b.py:9: changed  [status changed; candidate 12-13 (not verification)]" in out
    assert "unconfirmed: c.py changed" in out and "note: verify: c.py changed" in out
    assert "index refreshed first: incremental, 1 claim(s) marked stale" in out and "hint: scan --force" in out


def test_update_rendering_reports_derived_errors_and_pruned_files(capsys):
    from verinoda import cli

    cli._r_update({"mode": "incremental", "changed_count": 1, "index_mode": "incremental", "stale": [],
                   "derived": {"search_index": {"error": "OperationalError: locked"}, "lexicon": {"pairs": 3}},
                   "pruned_missing_files": ["gone.py"]})
    out = capsys.readouterr().out
    assert "warning: derived search_index not refreshed: OperationalError: locked" in out
    assert "lexicon" not in out and "pruned from the graph (files no longer exist): gone.py" in out


# -- runtime observation and precise resolution ------------------------------------------------------

def test_observe_selects_tests_and_prints_a_capped_summary(repo):
    r = ra("observe", "--for", "apply_discount", "OrderRepository.save", "--repo", str(repo), "--json", cwd=repo)
    assert r.returncode == 0, (r.stdout[-2000:], r.stderr[-2000:])
    res = json.loads(r.stdout)
    assert res["complete"] is True and res["run_id"] and res["tracer"] not in (None, "off")
    # static reach selects test_empty_order_rejected; the trace shows it never reaches apply_discount
    assert "tests/test_service.py::test_empty_order_rejected" in res["tests_selected"]
    reach = res["target_reach"]
    assert set(reach["apply_discount"]["tests"]) == GOLD_APPLY_DISCOUNT and reach["apply_discount"]["observed"]
    assert reach["OrderRepository.save"]["tests"] == ["tests/test_service.py::test_place_and_fetch_roundtrip"]
    assert any(b["site"] == "orders/repository.py:10" and b["callee"] == "sqlite3.connect"
               for b in res["boundary_for_targets"])
    assert "edges" not in res and "evidence" not in res and res["edges_total"] > 0 and len(r.stdout) < 8000
    assert res["limits"] and res["test_outcomes"] == {"passed": 5}
    r = ra("observe", "--for", "apply_discount", "--repo", str(repo), cwd=repo)
    assert r.returncode == 0 and "apply_discount: reached by 4 test(s)" in r.stdout and "limit: " in r.stdout
    r = ra("observe", "--for", "no_such_symbol_xyz", "--repo", str(repo), cwd=repo)
    assert r.returncode == 3 and "nothing was run" in r.stdout and "next: " in r.stdout
    # with the tracer off, "not reached" cannot be claimed
    r = ra("observe", "tests/test_pricing.py", "--for", "apply_discount", "--mode", "off", "--repo", str(repo),
           cwd=repo)
    assert r.returncode == 3 and "apply_discount: unknown - the tracer was off" in r.stdout


def test_observe_selection_and_arguments(repo, capsys, monkeypatch):
    from verinoda.runtime import trace as rt

    seen: list = []

    def fake(store, repo_, ids, **kw):
        seen.append((list(ids), kw))
        return {"run_id": "rtr_x", "complete": True, "tracer": "sys.monitoring", "tests": {}, "target_reach": {},
                "limits": ["run-scoped"], "edges": [{"big": 1}] * 500, "evidence": [{}] * 50, "edges_total": 500}

    monkeypatch.setattr(rt, "observe", fake)
    res = json.loads(run_cli(capsys, "observe", "--terms", "roundtrip", "--timeout", "30", "--mode", "setprofile",
                             "--repo", str(repo), "--json"))
    ids, kw = seen[-1]
    assert ids == ["tests/test_service.py::test_place_and_fetch_roundtrip"] and res["tests_selected"] == ids
    assert kw["timeout"] == 30.0 and kw["mode"] == "setprofile" and kw["targets"] == []
    assert kw["snapshot"]["id"] and "edges" not in res and res["evidence_records"] == 50
    run_cli(capsys, "observe", "tests/test_pricing.py", "--for", "apply_discount", "--repo", str(repo), "--json")
    ids, kw = seen[-1]
    assert ids == ["tests/test_pricing.py"] and kw["targets"] == ["apply_discount"] and kw["mode"] == "auto"
    run_cli(capsys, "observe", "--repo", str(repo), "--json")
    assert seen[-1][0] == []  # no ids, no selection: the whole suite
    r = ra("observe", "--mode", "fast", "--repo", str(repo), cwd=repo)
    assert r.returncode == 2 and "invalid choice" in r.stderr


def test_resolve_call_verdicts_and_no_answer(repo, capsys):
    from verinoda import precise

    if not precise.available()[0]:
        pytest.skip("jedi is not installed (verinoda[precise])")
    res = json.loads(run_cli(capsys, "resolve-call", "orders/pricing.py:8", "apply_discount", "--target",
                             "orders/pricing.py:11", "--repo", str(repo), "--json"))
    assert res["kind"] == "definitive" and res["verdict"] == "confirms"
    assert res["targets"][0]["path"] == "orders/pricing.py" and res["targets"][0]["line"] == 11
    # an absolute path is taken relative to the repository; a wrong target is a definitive refutation
    res = json.loads(run_cli(capsys, "resolve-call", f"{repo / 'orders' / 'pricing.py'}:8", "apply_discount",
                             "--target", "orders/service.py:19", "--repo", str(repo), "--json"))
    assert res["path"] == "orders/pricing.py" and res["verdict"] == "refutes"
    res = json.loads(run_cli(capsys, "resolve-call", "orders/service.py:22", "save", "--repo", str(repo), "--json"))
    assert res["kind"] == "dynamic" and "verdict" not in res  # a method on a parameter is never definitive
    out = run_cli(capsys, "resolve-call", "orders/service.py:22", "save", "--repo", str(repo))
    assert out.startswith("orders/service.py:22 save -> dynamic (jedi") and "reason: method called on a" in out
    out = run_cli(capsys, "resolve-call", "README.md:1", "anything", "--repo", str(repo), rc=3)
    assert out.startswith("no precise answer for README.md:1 anything:") and "--scip FILE" in out
    with pytest.raises(SystemExit, match="must look like"):
        run_cli(capsys, "resolve-call", "orders/pricing.py:eight", "x", "--repo", str(repo))
    with pytest.raises(SystemExit, match="outside the repository"):
        run_cli(capsys, "resolve-call", f"{repo.parent / 'elsewhere.py'}:1", "x", "--repo", str(repo))


def test_resolve_call_without_jedi_says_why(repo, capsys, monkeypatch):
    from verinoda import precise

    monkeypatch.setattr(precise, "resolve_call", lambda *a, **k: None)
    monkeypatch.setattr(precise, "available",
                        lambda: (False, "jedi is not installed (pip install 'verinoda[precise]')"))
    res = json.loads(run_cli(capsys, "resolve-call", "orders/pricing.py:8", "apply_discount", "--repo", str(repo),
                             "--json", rc=3))
    assert res["status"] == "no precise answer" and "jedi is not installed" in res["why"]
    assert res["next_step"] == "pip install 'verinoda[precise]'"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _ld(num: int, payload) -> bytes:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return _varint(num << 3 | 2) + _varint(len(data)) + data


def scip_index(docs: dict) -> bytes:
    """A minimal protobuf scip.Index: tool info + documents of (line0, col0, col1, symbol, roles)."""
    out = _ld(1, _ld(2, _ld(1, "scip-test") + _ld(2, "0.1")))
    for path, occs in docs.items():
        body = _ld(1, path)
        for line0, c0, c1, sym, roles in occs:
            body += _ld(2, _ld(1, b"".join(_varint(x) for x in (line0, c0, c1))) + _ld(2, sym)
                        + ((_varint(3 << 3) + _varint(roles)) if roles else b""))
        out += _ld(2, body)
    return out


def test_scan_precise_and_scip(tmp_path):
    import time

    dst = _git_copy(tmp_path / "orders_app")
    sym = "scip-python python orders 0.1 `orders.pricing`/apply_discount()."
    time.sleep(0.05)  # the index is written after the sources, as by a real indexer run
    idx = tmp_path / "made-by-indexer.scip"
    idx.write_bytes(scip_index({"orders/pricing.py": [(7, 11, 25, sym, 0), (10, 4, 18, sym, 1)],
                                "orders/gone.py": [(0, 0, 1, "local 1", 1)]}))
    res = ok_json("scan", str(dst), "--precise", "--scip", str(idx), "--json", cwd=dst)
    p = res["precise"]
    if p["available"]:
        assert p["basis"].startswith("all .py files") and p["files"] >= 8 and p["sites"] > 20
        assert p["kinds"]["definitive"] > 5 and "errors" not in p
    else:
        assert "jedi" in p["reason"]
    s = res["scip"]
    copied = dst / ".verinoda" / "index" / "index.scip"
    assert Path(s["index"]) == copied and copied.read_bytes() == idx.read_bytes()
    assert copied.stat().st_mtime_ns == idx.stat().st_mtime_ns  # kept: freshness compares it with file times
    assert (s["documents"], s["fresh"], s["stale"]) == (2, 1, ["orders/gone.py"]) and s["tool"] == "scip-test 0.1"
    doc = ok_json("doctor", "--repo", str(dst), "--json", cwd=dst)
    assert doc["project"]["scip"]["documents"] == 2 and doc["project"]["scip"]["fresh_share"] == 0.5
    # nothing changed: nothing resolved again; one edited file: only that file
    again = ok_json("scan", str(dst), "--precise", "--json", cwd=dst)
    if p["available"]:
        assert again["precise"]["files"] == 0 and again["precise"]["basis"].startswith("changed .py files")
    f = dst / "orders" / "service.py"
    f.write_bytes(f.read_bytes() + b"\n\ndef later():\n    return fetch_order(None, 1)\n")
    r = ra("scan", str(dst), "--precise", cwd=dst)
    assert r.returncode == 0 and "  scip:" not in r.stdout
    if p["available"]:
        assert "in 1 file(s) [changed .py files since the previous snapshot]" in r.stdout
    else:
        assert "precise: not run" in r.stdout
    # a file that is not a SCIP index fails before anything is scanned or created
    empty = tmp_path / "empty"
    empty.mkdir()
    (tmp_path / "bad.scip").write_bytes(b"garbage-not-scip")
    for arg, msg in ((tmp_path / "bad.scip", "not a readable SCIP index"), (tmp_path / "no.scip", "does not exist")):
        r = ra("scan", str(empty), "--scip", str(arg), cwd=empty)
        assert r.returncode == 1 and msg in r.stderr and "Traceback" not in r.stderr
    assert list(empty.iterdir()) == []


# -- `verinoda index`: nothing under ~/.graphify -------------------------------------------------------

@pytest.mark.parametrize("argv", [["clone", "https://github.com/a/b"], ["provider", "list"],
                                  ["global", "add", "g.json"], ["extract", ".", "--global"],
                                  ["update", ".", "--global=tag"]])
def test_index_blocks_commands_that_write_under_home(argv, monkeypatch, capsys):
    from verinoda import cli

    calls = _fake_upstream(monkeypatch)
    for full in (["index", "--", *argv], ["index", *argv]):
        assert cli.main(full) == 2, full
        err = capsys.readouterr().err
        assert "is blocked" in err and "~/.graphify" in err and "verinoda research" in err
    assert calls == [] and sys.argv == ["pytest"]
    assert not set(argv) & set(cli._blocked_index_commands())  # its own reason, not the installer one


def test_index_home_writers_blocked_end_to_end(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    env = _env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData"))
    for args in (("index", "--", "clone", "https://github.com/a/b"), ("index", "--", "provider", "list"),
                 ("index", "--", "global", "list")):
        r = ra(*args, cwd=work, env=env)
        assert r.returncode == 2 and "~/.graphify" in r.stderr and "Traceback" not in r.stderr, args
    assert list(home.iterdir()) == [] and list(work.iterdir()) == []


# -- benchmark harnesses ------------------------------------------------------------------------------

def test_benchmark_staleness_subcommands(tmp_path, capsys, monkeypatch):
    from verinoda.benchmark import staleness

    fake = {"schema": "x", "all_ok": True, "stale_recall": 1.0, "rows": [{"category": "a"}] * 3}
    monkeypatch.setattr(staleness, "run_mutation_suite", lambda: fake)
    out = tmp_path / "m.json"
    res = json.loads(run_cli(capsys, "benchmark", "staleness", "mutations", "--out", str(out), "--json"))
    assert res["rows_omitted"] == 3 and "rows" not in res and res["full_result"] == str(out)
    assert json.loads(out.read_bytes()) == fake and b"\r" not in out.read_bytes()
    monkeypatch.setattr(staleness, "run_mutation_suite", lambda: {**fake, "all_ok": False})
    run_cli(capsys, "benchmark", "staleness", "mutations", rc=1)
    seen: dict = {}

    def replay(corpus, **kw):
        seen.update(kw, corpus=corpus)
        return {"recall_ok": True, "silent_wrong_ok": False, "overall": {"file_mode": {"recall": 1.0}}}

    monkeypatch.setattr(staleness, "replay", replay)
    text = run_cli(capsys, "benchmark", "staleness", "replay", "--repo", str(tmp_path), "--commits", "5", "--cap",
                   "0", "--pathspec", "src/*.py", rc=1)
    assert seen["commits"] == 5 and seen["cap_per_kind"] is None and seen["pathspec"] == "src/*.py"
    assert seen["corpus"] == tmp_path.resolve() and callable(seen["progress"])
    assert text.startswith("staleness replay") and "overall.file_mode: recall=1.0" in text


def test_benchmark_critique_eval_and_sanitize(tmp_path, capsys):
    out = tmp_path / "c.json"
    res = json.loads(run_cli(capsys, "benchmark", "critique-eval", "--out", str(out), "--json"))
    assert res["benchmark_negatives"]["stated_as_verified"] == 0 and "rows" not in res
    assert res["rows_omitted"] == res["cases"] == len(json.loads(out.read_text(encoding="utf-8"))["rows"])
    text = run_cli(capsys, "benchmark", "critique-eval")
    assert text.startswith("critique evaluation (in-sample labelled set)") and "benchmark_negatives." in text
    f = tmp_path / "res.json"
    f.write_bytes(json.dumps({"questions": [], "note": str(tmp_path / "work" / "x.py")}).encode("utf-8"))
    res = json.loads(run_cli(capsys, "benchmark", "sanitize", str(f), "--json"))
    assert res[0]["file"] == "res.json" and res[0]["json_changed"] is True
    body = f.read_text(encoding="utf-8")
    assert str(tmp_path) not in body and "<TMP>/" in body
    assert run_cli(capsys, "benchmark", "sanitize", str(f)).startswith("res.json: json changed False")


# -- from install to the first answer ---------------------------------------------------------------

def _git_copy(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache",
                                                                "*.db"))
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=dst, check=True, capture_output=True)
    return dst


def test_the_first_question_in_a_project_indexes_it(tmp_path, capsys, monkeypatch):
    from verinoda import cli

    monkeypatch.delenv("VERINODA_NO_AUTO_INDEX", raising=False)
    repo = _git_copy(tmp_path / "app")
    assert cli.main(["query", "Where is an order written to the database?", "--repo", str(repo)]) == 0
    cap = capsys.readouterr()
    assert "orders/" in cap.out and (repo / ".verinoda" / "index" / "graph.json").is_file()
    assert "first use in" in cap.err and "indexing it once" in cap.err  # said, on stderr


def test_no_index_is_made_outside_a_project_or_when_turned_off(tmp_path, capsys, monkeypatch):
    from verinoda import cli

    plain = tmp_path / "plain"
    shutil.copytree(EXAMPLE, plain, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    with pytest.raises(SystemExit, match="not a git work tree"):
        cli.main(["query", "anything", "--repo", str(plain)])
    assert not (plain / ".verinoda" / "index" / "graph.json").exists()
    monkeypatch.setenv("VERINODA_NO_AUTO_INDEX", "1")
    repo = _git_copy(tmp_path / "app")
    with pytest.raises(SystemExit, match="has no index yet"):
        cli.main(["query", "anything", "--repo", str(repo)])
