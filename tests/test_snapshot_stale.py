"""Snapshots and stale invalidation on a scanned, git-committed copy of examples/orders_app.

Covers: modify / commit / delete a cited file -> exactly the dependent claims go
stale; revert + verify restores up to the ceiling and re-binds to a snapshot of
the current tree; memories derived from stale claims are invalidated, never
deleted; an added (restored) file re-resolves its dependents' edges.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import evidence as evmod  # noqa: E402
from verinoda import index, workflow  # noqa: E402
from verinoda.claims import CONFIDENCE_CAP, Claims  # noqa: E402
from verinoda.memory import Memory  # noqa: E402
from verinoda.snapshot import (  # noqa: E402
    changed_files,
    current_state,
    git_info,
    hash_files,
    list_files,
    take_snapshot,
)
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                       cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8")
    return r.stdout


def _copy_example(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _line(repo: Path, rel: str, needle: str) -> int:
    return next(i for i, t in enumerate((repo / rel).read_text(encoding="utf-8").splitlines(), 1) if needle in t)


def _edit(repo: Path, rel: str, old: str, new: str) -> bytes:
    p = repo / rel
    before = p.read_bytes()
    text = before.decode("utf-8")
    assert old in text
    p.write_bytes(text.replace(old, new).encode("utf-8"))
    return before


@pytest.fixture
def proj(tmp_path):
    repo = _copy_example(tmp_path / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    snap = st.latest_snapshot()
    cl = Claims(st, repo)

    def claim(text, rel, a, b=None, status="statically_verified", subjects=None):
        ev = evmod.source_evidence(repo, rel, a, b or a, commit=snap["commit_sha"])
        assert ev is not None
        return cl.create(text, project=snap["project"], snapshot=snap, status=status,
                         evidence=[(ev, "supports")], subjects=subjects or [rel], actor="test")

    a = _line(repo, "orders/pricing.py", "def compute_total")
    claims = {
        "pricing": claim("compute_total sums price*qty", "orders/pricing.py", a, a + 2),
        "pricing_inference": claim("compute_total is the only pricing entry", "orders/pricing.py", a, a + 2,
                                   status="strong_inference"),
        "service": claim("place_order calls validate_items", "orders/service.py",
                         _line(repo, "orders/service.py", "validate_items(items)")),
        "repository": claim("save inserts into orders", "orders/repository.py",
                            _line(repo, "orders/repository.py", "INSERT INTO")),
        "config": claim("the discount threshold is read from ORDERS_DISCOUNT_THRESHOLD", "orders/config.py",
                        _line(repo, "orders/config.py", "ORDERS_DISCOUNT_THRESHOLD")),
    }
    yield repo, st, cl, claims
    st.close()


def _status(cl, claims) -> dict:
    return {k: cl.get(c["id"])["status"] for k, c in claims.items()}


def _hist_len(cl, claims) -> dict:
    return {k: len(cl.show(c["id"])["history"]) for k, c in claims.items()}


# -- snapshots -----------------------------------------------------------------------------

def test_snapshot_records_commit_files_and_is_reused_for_an_unchanged_tree(proj):
    repo, st, cl, claims = proj
    snap = st.latest_snapshot()
    info = git_info(repo)
    assert snap["commit_sha"] == info["commit"] and len(snap["commit_sha"]) == 40
    assert snap["dirty"] == 0 and snap["graph_nodes"] > 0 and snap["graph_path"].endswith("graph.json")
    files = st.snapshot_files(snap["id"])
    assert "orders/pricing.py" in files and not any(f.startswith(".verinoda") for f in files)
    assert files == hash_files(repo) and snap["file_count"] == len(files) == len(list_files(repo))
    assert take_snapshot(st, repo)["id"] == snap["id"]  # nothing changed -> same snapshot
    assert workflow.update(st, repo)["mode"] == "noop"


def test_changed_files_classifies_added_removed_modified():
    assert changed_files({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "9", "d": "4"}) == {
        "added": ["d"], "removed": ["c"], "modified": ["b"]}


def test_list_files_without_git_skips_tool_dirs(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    for junk in (".verinoda", "__pycache__", ".venv", "node_modules"):
        (tmp_path / junk).mkdir()
        (tmp_path / junk / "f.py").write_text("y = 2\n", encoding="utf-8")
    assert list_files(tmp_path) == ["pkg/m.py"]
    assert git_info(tmp_path)["is_git"] is False


# -- stale invalidation ----------------------------------------------------------------------

def test_modifying_a_cited_file_marks_exactly_its_claims_stale(proj):
    repo, st, cl, claims = proj
    hist = _hist_len(cl, claims)
    _edit(repo, "orders/pricing.py", 'i["price"] * i["qty"]', 'i["price"] * i["qty"] * 2')
    res = workflow.update(st, repo)
    assert res["mode"] == "incremental" and res["changed"]["modified"] == ["orders/pricing.py"]
    stale_ids = {s["id"] for s in res["stale"]}
    assert stale_ids == {claims["pricing"]["id"], claims["pricing_inference"]["id"]}
    for s in res["stale"]:
        assert s["changed"] == [{"file": "orders/pricing.py", "change": "modified"}]
    st_now = _status(cl, claims)
    assert st_now["pricing"] == st_now["pricing_inference"] == "stale"
    assert st_now["service"] == st_now["repository"] == st_now["config"] == "statically_verified"
    after = _hist_len(cl, claims)
    for k in ("service", "repository", "config"):
        assert after[k] == hist[k], f"{k} history must be untouched"
    h = cl.show(claims["pricing"]["id"])["history"][-1]
    assert h["to_status"] == "stale" and "changed since snapshot" in h["reason"]
    assert cl.get(claims["pricing"]["id"])["confidence"] == CONFIDENCE_CAP["stale"]
    # A second update is a no-op and does not touch history again.
    assert workflow.update(st, repo)["mode"] == "noop"
    assert _hist_len(cl, claims) == after


def test_a_new_commit_that_changes_a_file_marks_its_claims_stale(proj):
    repo, st, cl, claims = proj
    old_commit = st.latest_snapshot()["commit_sha"]
    _edit(repo, "orders/service.py", "validate_items(items)\n", "validate_items(items)  # checked\n")
    _git(repo, "commit", "-q", "-am", "annotate service")
    res = workflow.update(st, repo)
    assert res["snapshot"]["commit_sha"] != old_commit and res["snapshot"]["dirty"] == 0
    assert {s["id"] for s in res["stale"]} == {claims["service"]["id"]}
    reason = cl.show(claims["service"]["id"])["history"][-1]["reason"]
    assert f"commit {old_commit[:10]} -> {res['snapshot']['commit_sha'][:10]}" in reason
    assert _status(cl, claims)["pricing"] == "statically_verified"
    # A commit that changes no file content creates a snapshot but marks nothing stale.
    _git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    res2 = workflow.update(st, repo)
    assert res2["mode"] == "incremental" and res2["changed_count"] == 0 and res2["stale"] == []


def test_deleting_a_cited_file_marks_its_claims_stale(proj):
    repo, st, cl, claims = proj
    (repo / "orders" / "repository.py").unlink()
    res = workflow.update(st, repo)
    assert res["changed"]["removed"] == ["orders/repository.py"]
    assert {s["id"] for s in res["stale"]} == {claims["repository"]["id"]}
    assert res["stale"][0]["changed"] == [{"file": "orders/repository.py", "change": "removed"}]
    assert _status(cl, claims)["repository"] == "stale"
    v = workflow.verify(st, repo, claims["repository"]["id"])
    assert v["after"]["status"] == "stale"
    assert v["source_checks"][0]["reason"] == "file no longer exists: orders/repository.py"


def test_revert_then_verify_restores_up_to_ceiling_and_rebinds(proj):
    repo, st, cl, claims = proj
    s1 = st.latest_snapshot()
    original = _edit(repo, "orders/pricing.py", 'i["price"] * i["qty"]', 'i["price"] + i["qty"]')
    s2 = workflow.update(st, repo)["snapshot"]
    assert _status(cl, claims)["pricing"] == "stale"

    # Verify while still modified: cited lines changed -> stays stale, not rebound.
    v = workflow.verify(st, repo, claims["pricing"]["id"])
    assert v["after"]["status"] == "stale" and v["source_checks"][0]["reason"].startswith("cited lines changed")
    assert cl.get(claims["pricing"]["id"])["snapshot_id"] == s1["id"]

    (repo / "orders" / "pricing.py").write_bytes(original)  # revert (no update in between)
    v = workflow.verify(st, repo, claims["pricing"]["id"])
    assert v["before"]["status"] == "stale"
    assert v["after"] == {"status": "statically_verified", "confidence": CONFIDENCE_CAP["statically_verified"]}
    assert v["index_refresh"]["mode"] == "incremental"  # verify refreshed the snapshot of the reverted tree
    c = cl.get(claims["pricing"]["id"])
    assert c["snapshot_id"] not in (s1["id"], s2["id"]) and c["snapshot_id"] == st.latest_snapshot()["id"]
    assert st.snapshot(c["snapshot_id"])["tree_hash"] == current_state(repo)["tree_hash"]
    assert v["snapshot"]["id"] == c["snapshot_id"]
    hist = cl.show(claims["pricing"]["id"])["history"]
    assert [h["to_status"] for h in hist][-3:] == ["stale", "statically_verified", "statically_verified"]
    assert hist[-1]["reason"] == "verify: rebound to current snapshot"

    # The inference claim citing the same lines is restored only up to its ceiling.
    vi = workflow.verify(st, repo, claims["pricing_inference"]["id"])
    assert vi["after"]["status"] == "strong_inference"
    # Once re-bound, a later unrelated update leaves the restored claim alone.
    _edit(repo, "orders/config.py", '"orders.db"', '"orders2.db"')
    assert claims["pricing"]["id"] not in {s["id"] for s in workflow.update(st, repo)["stale"]}


def test_verify_does_not_restore_when_a_subject_file_changed_without_recheckable_evidence(proj):
    repo, st, cl, claims = proj
    snap = st.latest_snapshot()
    # A test-run claim: its only evidence is an (old) passing run, its subjects are files.
    run_ev = {"source_type": "test_result", "locator": "run exp_old: pytest tests/test_pricing.py",
              "content_hash": "sha256:0", "meta": {"outcome": "pass", "experiment_id": "exp_old"}}
    c = cl.create("pricing tests pass", project=snap["project"], snapshot=snap, status="experiment_verified",
                  evidence=[(run_ev, "supports")], subjects=["orders/pricing.py", "tests/test_pricing.py"],
                  kind="test_run", spec={"command": ["tests/test_pricing.py"], "experiment": "exp_old"})
    assert c["status"] == "experiment_verified"
    _edit(repo, "orders/pricing.py", "round(subtotal * 0.9, 2)", "round(subtotal * 0.5, 2)")
    v = workflow.verify(st, repo, c["id"])
    # The old run says nothing about the changed code: it must not be carried over.
    assert v["after"]["status"] == "stale" and v["unconfirmed_files"] == ["orders/pricing.py"]
    assert cl.get(c["id"])["snapshot_id"] == snap["id"]


def test_memory_from_a_stale_claim_is_invalidated_not_deleted(proj):
    repo, st, cl, claims = proj
    mem = Memory(st)
    mem.learn("pricing.entry", "orders/pricing.py::compute_total", source_claim_id=claims["pricing"]["id"])
    mem.learn("persistence.owner", "orders/repository.py", source_claim_id=claims["repository"]["id"])
    rows_before = st.one("SELECT COUNT(*) AS n FROM memory")["n"]
    _edit(repo, "orders/pricing.py", "return apply_discount(subtotal)", "return subtotal")
    workflow.update(st, repo)
    assert mem.recall("pricing.entry") == []
    assert [m["value"] for m in mem.recall("persistence.owner")] == ["orders/repository.py"]
    hist = mem.history("pricing.entry")
    assert len(hist) == 1 and hist[0]["valid"] == 0 and hist[0]["invalidated_at"]
    assert hist[0]["invalidation_reason"] == "source claim became stale"
    assert st.one("SELECT COUNT(*) AS n FROM memory")["n"] == rows_before
    # Learning from a stale claim is refused (recall must only return standing facts).
    with pytest.raises(ValueError, match="stale"):
        mem.learn("pricing.entry", "again", source_claim_id=claims["pricing"]["id"])


def test_claims_citing_other_checkouts_do_not_go_stale_on_local_changes(proj, tmp_path):
    repo, st, cl, claims = proj
    ext = tmp_path / "ref"
    (ext / "orders").mkdir(parents=True)
    (ext / "orders" / "pricing.py").write_text("def compute_total(items):\n    return 0\n", encoding="utf-8")
    snap = st.latest_snapshot()
    ev = evmod.source_evidence(ext, "orders/pricing.py", 1, 2, commit="refsha", source_type="reference_repo",
                               meta={"root": str(ext)})
    c = cl.create("compute_total returns 0", project=snap["project"], snapshot=snap,
                  status="statically_verified", evidence=[(ev, "supports")])
    assert c["status"] == "statically_verified"
    _edit(repo, "orders/pricing.py", "return apply_discount(subtotal)", "return subtotal")
    stale = {s["id"] for s in workflow.update(st, repo)["stale"]}
    assert c["id"] not in stale and claims["pricing"]["id"] in stale


def test_restoring_a_deleted_file_re_resolves_dependents(proj):
    repo, st, cl, claims = proj
    full = st.latest_snapshot()["graph_edges"]
    original = (repo / "orders" / "repository.py").read_bytes()
    (repo / "orders" / "repository.py").unlink()
    res = workflow.update(st, repo)
    assert res["index_mode"] == "full" and res["snapshot"]["graph_edges"] < full
    (repo / "orders" / "repository.py").write_bytes(original)
    res = workflow.update(st, repo)
    # Unchanged files (api.py, service.py, tests) import the restored module; an
    # incremental pass alone would leave those cross-file edges unresolved.
    assert res["index_mode"] == "full" and res["changed"]["added"] == ["orders/repository.py"]
    assert res["snapshot"]["graph_edges"] == full
    g = index.load(repo)
    assert any(g.label(u) == "get_repo()" and g.label(v) == "OrderRepository"
               for u, v, d in g.edges({"calls"}))


# -- claims that watch the test suite ----------------------------------------------------------

def _watch_claim(cl, st, text="No test statically reaches `compute_total`"):
    snap = st.latest_snapshot()
    return cl.create(text, project=snap["project"], snapshot=snap, status="weak_inference", kind="tests",
                     subjects=["orders/pricing.py::compute_total"], spec={"watch": "tests"}, actor="test")


def test_watch_tests_claims_go_stale_when_a_test_file_is_added_changed_or_removed(proj):
    repo, st, cl, claims = proj
    tests_dir = repo / "tests"
    existing = sorted(p.name for p in tests_dir.glob("test_*.py"))
    assert existing, "the example has tests"
    w1 = _watch_claim(cl, st)
    (tests_dir / "test_new_probe.py").write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    (repo / "orders" / "notes.py").write_text("X = 1\n", encoding="utf-8")  # a non-test file: not watched
    res = workflow.update(st, repo)
    assert res["changed"]["added"] == ["orders/notes.py", "tests/test_new_probe.py"]
    by_id = {s["id"]: s for s in res["stale"]}
    assert set(by_id) == {w1["id"]}  # ordinary claims ignore new files; the watcher does not
    assert by_id[w1["id"]]["changed"] == [{"file": "tests/test_new_probe.py", "change": "added"}]
    assert cl.get(w1["id"])["status"] == "stale"

    w2 = _watch_claim(cl, st, "watcher 2")
    _edit(repo, f"tests/{existing[0]}", "def test_", "def test_renamed_")
    res = workflow.update(st, repo)
    assert {s["id"] for s in res["stale"]} == {w2["id"]}
    assert res["stale"][0]["changed"] == [{"file": f"tests/{existing[0]}", "change": "modified"}]

    w3 = _watch_claim(cl, st, "watcher 3")
    (tests_dir / "test_new_probe.py").unlink()
    res = workflow.update(st, repo)
    assert {s["id"] for s in res["stale"]} == {w3["id"]}
    assert res["stale"][0]["changed"] == [{"file": "tests/test_new_probe.py", "change": "removed"}]
    # verify does not restore a watcher while the test set differs from its snapshot
    assert workflow.verify(st, repo, w1["id"])["after"]["status"] == "stale"


# -- the indexer refuses to rewrite the graph --------------------------------------------------

def test_refused_index_rebuild_records_no_snapshot_and_says_what_to_do(proj, monkeypatch):
    repo, st, cl, claims = proj
    prev = st.latest_snapshot()
    n_snaps = st.one("SELECT COUNT(*) AS n FROM snapshots")["n"]
    real_build = index.build

    def refusing_build(repo_, **kw):
        return {"ok": False, "graph_path": prev["graph_path"], "nodes": prev["graph_nodes"],
                "edges": prev["graph_edges"], "log": "[graphify] refusing to overwrite: graph would shrink"}

    monkeypatch.setattr(workflow.index, "build", refusing_build)
    (repo / "orders" / "repository.py").unlink()
    res = workflow.update(st, repo)
    assert res["snapshot"]["id"] == prev["id"] and res["mode"] == "index_refused"
    assert "did not rewrite the graph" in res["error"] and res["hint"] == f"verinoda scan {repo} --force"
    assert st.one("SELECT COUNT(*) AS n FROM snapshots")["n"] == n_snaps  # nothing recorded over the old graph
    # Claims on the deleted file are still invalidated (that needs file hashes, not the graph).
    assert {s["id"] for s in res["stale"]} == {claims["repository"]["id"]}
    assert _status(cl, claims)["repository"] == "stale"
    # verify does not re-bind anything to the out-of-date snapshot
    v = workflow.verify(st, repo, claims["pricing"]["id"])
    assert v["index_refresh"]["error"] and v["index_refresh"]["hint"].endswith("--force")
    assert v["after"]["status"] == "statically_verified" and v["snapshot"]["id"] == prev["id"]

    s = workflow.scan(st, repo)
    assert s["error"] and s["hint"] and s["snapshot"]["id"] == prev["id"]
    assert st.one("SELECT COUNT(*) AS n FROM snapshots")["n"] == n_snaps

    monkeypatch.setattr(workflow.index, "build", real_build)
    forced = workflow.scan(st, repo, force=True)
    assert "error" not in forced and forced["snapshot"]["id"] != prev["id"]
    assert forced["snapshot"]["graph_edges"] < prev["graph_edges"]
    assert "orders/repository.py" not in st.snapshot_files(forced["snapshot"]["id"])


# -- symbol/facet-level invalidation (DESIGN D24) -------------------------------------------------

def _loc_claim(repo, st, cl, sym, rel, needle, n):
    snap = st.latest_snapshot()
    a = _line(repo, rel, needle)
    ev = evmod.source_evidence(repo, rel, a, a + n - 1, commit=snap["commit_sha"], meta={"symbol": sym})
    return cl.create(f"`{sym}` is defined at {rel}:{a}-{a + n - 1}", project=snap["project"], snapshot=snap,
                     status="statically_verified", evidence=[(ev, "supports")], kind="location",
                     subjects=[f"{rel}::{sym}"], actor="test")


def _rel_claim(repo, st, cl, caller, target, rel, needle, target_file):
    snap = st.latest_snapshot()
    ln = _line(repo, rel, needle)
    ev = evmod.source_evidence(repo, rel, ln, commit=snap["commit_sha"])
    return cl.create(f"`{caller}` calls `{target}` ({rel}:{ln})", project=snap["project"], snapshot=snap,
                     status="statically_verified", evidence=[(ev, "supports")], kind="relation",
                     subjects=[f"{rel}::{caller}", f"{target_file}::{target}"], actor="test",
                     spec={"target_label": target, "relation": "calls", "at": f"{rel}:{ln}",
                           "confidence": "EXTRACTED"})


@pytest.fixture
def sym(proj):
    repo, st, cl, claims = proj
    out = {
        "compute": _loc_claim(repo, st, cl, "compute_total()", "orders/pricing.py", "def compute_total", 3),
        "apply": _loc_claim(repo, st, cl, "apply_discount()", "orders/pricing.py", "def apply_discount", 5),
        "calls_apply": _rel_claim(repo, st, cl, "compute_total()", "apply_discount()", "orders/pricing.py",
                                  "return apply_discount(subtotal)", "orders/pricing.py"),
        "calls_total": _rel_claim(repo, st, cl, "place_order()", "compute_total()", "orders/service.py",
                                  "total = compute_total(items)", "orders/pricing.py"),
    }
    assert all(c["status"] == "statically_verified" for c in out.values()), {k: c["status"] for k, c in out.items()}
    assert all(st.claim_deps(c["id"]) for c in out.values())
    return repo, st, cl, claims, out


def test_claim_dependencies_are_symbol_facets(sym):
    repo, st, cl, claims, s = sym
    deps = {(d["dep_key"], d["facet"]) for d in st.claim_deps(s["calls_total"]["id"])}
    assert ("sym:orders/service.py::place_order", "body") in deps           # the caller's body
    assert ("bind:orders/service.py::compute_total", "bind") in deps        # what the name is bound to
    assert ("sym:orders/pricing.py::compute_total", "sig") in deps          # the callee exists as declared
    assert {(d["dep_key"], d["facet"]) for d in st.claim_deps(s["compute"]["id"])} == \
        {("sym:orders/pricing.py::compute_total", "sig")}
    assert {d["dep_key"] for d in st.claim_deps(claims["pricing"]["id"])} == {
        "sym:orders/pricing.py::compute_total", "file:orders/pricing.py"}  # a plain file subject stays file-level


def test_shift_edit_rebinds_symbol_claims_instead_of_staling_them(sym):
    repo, st, cl, claims, s = sym
    old_snap = st.latest_snapshot()["id"]
    _edit(repo, "orders/pricing.py", '"""Pricing rules."""\n', '"""Pricing rules."""\n# moved down by one line\n')
    report: dict = {}
    from verinoda.claims import invalidate_stale

    res = workflow.update(st, repo)
    stale = {c["id"] for c in res["stale"]}
    assert claims["pricing"]["id"] in stale  # file-level subject: stale as before
    for k in ("compute", "apply", "calls_apply"):
        c = cl.get(s[k]["id"])
        assert s[k]["id"] not in stale and c["status"] == "statically_verified", k
        assert c["snapshot_id"] == res["snapshot"]["id"] != old_snap, k
        h = st.history(s[k]["id"])[-1]
        assert h["reason"].startswith("rebound to snapshot") and h["from_status"] == h["to_status"], k
        eid = cl.evidence(s[k]["id"])[0]["id"]
        assert st.latest_evidence_location(eid)["status"] == "moved", k
    # place_order -> compute_total depends on compute_total's signature in pricing.py: rebound too
    assert cl.get(s["calls_total"]["id"])["snapshot_id"] == res["snapshot"]["id"]
    # a claim that depends on nothing in pricing.py is left alone (no history noise)
    n_hist = len(st.history(claims["repository"]["id"]))
    assert cl.get(claims["repository"]["id"])["snapshot_id"] == old_snap and n_hist == 2
    assert cl.show(s["compute"]["id"])["supporting"][0]["now"].endswith("(moved)")
    assert invalidate_stale(st, res["snapshot"], report=report) == [] and report["rebound"] == 0


def test_body_edit_stales_only_claims_that_depend_on_it(sym):
    repo, st, cl, claims, s = sym
    _edit(repo, "orders/pricing.py", "round(subtotal * 0.9, 2)", "round(subtotal * 0.8, 2)")
    res = workflow.update(st, repo)
    stale = {c["id"]: c for c in res["stale"]}
    assert s["apply"]["id"] in stale  # its cited lines changed
    assert s["compute"]["id"] not in stale and s["calls_apply"]["id"] not in stale  # apply's signature did not
    assert cl.get(s["calls_apply"]["id"])["status"] == "statically_verified"
    assert stale[s["apply"]["id"]]["changed"] == [{"file": "orders/pricing.py", "change": "modified"}]


def test_renamed_target_stales_the_relation_and_the_callers_location(sym):
    repo, st, cl, claims, s = sym
    p = repo / "orders" / "pricing.py"
    p.write_bytes(p.read_bytes().replace(b"apply_discount", b"apply_rebate"))
    res = workflow.update(st, repo)
    stale = {c["id"]: c for c in res["stale"]}
    assert {s["calls_apply"]["id"], s["apply"]["id"], s["compute"]["id"]} <= set(stale)
    facets = {f.get("dep") for f in stale[s["calls_apply"]["id"]]["facets"]}
    assert "sym:orders/pricing.py::apply_discount" in facets
    assert s["calls_total"]["id"] not in stale


def test_rebinding_the_called_name_stales_the_relation(sym):
    """The call line and the caller's body are unchanged, but the name now means another function."""
    repo, st, cl, claims, s = sym
    _edit(repo, "orders/service.py", "from orders.pricing import compute_total\n",
          "from orders.pricing import apply_discount as compute_total\n")
    res = workflow.update(st, repo)
    stale = {c["id"]: c for c in res["stale"]}
    assert s["calls_total"]["id"] in stale
    assert any(f.get("dep") == "bind:orders/service.py::compute_total" for f in stale[s["calls_total"]["id"]]["facets"])
    assert s["compute"]["id"] not in stale


def test_missing_facts_fall_back_to_file_level(sym, monkeypatch):
    repo, st, cl, claims, s = sym
    from verinoda import claims as claims_mod

    monkeypatch.setattr(claims_mod.anchors, "facts_by_sha", lambda *a, **k: None)
    monkeypatch.setattr(claims_mod.anchors, "facts_for", lambda *a, **k: None)
    _edit(repo, "orders/pricing.py", '"""Pricing rules."""\n', '"""Pricing rules."""\n# shift\n')
    res = workflow.update(st, repo)
    stale = {c["id"]: c for c in res["stale"]}
    assert {s["compute"]["id"], s["apply"]["id"], s["calls_apply"]["id"]} <= set(stale)
    assert any("file-level fallback" in (f.get("why") or "") for f in stale[s["compute"]["id"]]["facets"])


def test_cited_file_outside_the_snapshot_is_rechecked(proj):
    """Audit criterion 7: a cited file that git ignores is not in any snapshot, yet an edit must stale its claims."""
    repo, st, cl, claims = proj
    (repo / ".gitignore").write_text("orders/local_settings.py\n", encoding="utf-8")
    (repo / "orders" / "local_settings.py").write_text("DEBUG = False\n", encoding="utf-8")
    snap = workflow.update(st, repo)["snapshot"]
    assert "orders/local_settings.py" not in st.snapshot_files(snap["id"])
    ev = evmod.source_evidence(repo, "orders/local_settings.py", 1, commit=snap["commit_sha"])
    c = cl.create("DEBUG is False", project=snap["project"], snapshot=snap, status="statically_verified",
                  evidence=[(ev, "supports")], actor="test")
    assert c["status"] == "statically_verified"
    (repo / "orders" / "local_settings.py").write_text("DEBUG = True\n", encoding="utf-8")
    from verinoda import critique

    res = critique.challenge(st, repo, c["id"])
    assert {f["check"]: f["result"] for f in res["findings"]}["staleness"] == "fail"
    d = cl.create("DEBUG flag in local_settings", project=snap["project"], snapshot=snap, status="strong_inference",
                  evidence=[(evmod.source_evidence(repo, "orders/local_settings.py", 1, commit=None), "supports")])
    (repo / "orders" / "local_settings.py").write_text("DEBUG = False\n", encoding="utf-8")
    _edit(repo, "orders/config.py", '"orders.db"', '"orders2.db"')  # any tracked change triggers invalidation
    assert d["id"] in {x["id"] for x in workflow.update(st, repo)["stale"]}


def test_claim_text_and_birth_are_immutable(proj):
    import sqlite3

    repo, st, cl, claims = proj
    cid = claims["pricing"]["id"]
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        st.conn.execute("UPDATE claims SET text = 'rewritten' WHERE id = ?", (cid,))
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        st.conn.execute("UPDATE claims SET created_at = '2000-01-01' WHERE id = ?", (cid,))
    st.conn.rollback()
    st.update_claim(cid, {"uncertainties": ["other fields still change"]})
    assert cl.get(cid)["text"] == "compute_total sums price*qty"
