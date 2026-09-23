"""Tests for verinoda.feedback on a copy of examples/orders_app (never touches examples/ itself)."""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import evidence as evmod  # noqa: E402
from verinoda import feedback  # noqa: E402
from verinoda import workflow  # noqa: E402
from verinoda.claims import Claims  # noqa: E402
from verinoda.store import open_store  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "orders_app"
UNREACHABLE_REPO = "https://127.0.0.1:9/acme/orders.git"  # refused port: fails fast, no network needed

REF_REPOSITORY = '''"""Persistence with a connection lock and a busy timeout."""
import os
import sqlite3
import threading

_DB_LOCK = threading.Lock()
TIMEOUT = float(os.environ.get("ORDERS_DB_TIMEOUT", "5"))


class OrderRepository:
    def __init__(self, url: str = "orders.db"):
        self.conn = sqlite3.connect(url, timeout=TIMEOUT, check_same_thread=False)

    def save(self, customer: str, total: float) -> int:
        with _DB_LOCK:
            try:
                cur = self.conn.execute("INSERT INTO orders (customer, total) VALUES (?, ?)", (customer, total))
                self.conn.commit()
            except sqlite3.OperationalError:
                self.conn.rollback()
                raise
        return cur.lastrowid
'''

CACHE = '''"""Read-through cache kept next to the orders database."""
import sqlite3


def open_cache(path: str = ":memory:"):
    return sqlite3.connect(path)
'''


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
                       capture_output=True, text=True, encoding="utf-8", check=True)
    return r.stdout.strip()


@pytest.fixture
def orders(tmp_path):
    """Scanned git copy of examples/orders_app + an exclusive claim about sqlite3."""
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "orders app")
    st = open_store(repo)
    snap = workflow.scan(st, repo)["snapshot"]
    evs = [(evmod.source_evidence(repo, "orders/repository.py", 3, commit=snap["commit_sha"]), "supports"),
           (evmod.source_evidence(repo, "orders/repository.py", 10, commit=snap["commit_sha"]), "supports")]
    assert all(e for e, _ in evs)
    c = Claims(st, repo).create(
        "Only orders/repository.py calls sqlite3", project=snap["project"], snapshot=snap,
        subjects=["orders/repository.py"], status="statically_verified", evidence=evs, kind="exclusive",
        spec={"pattern": "sqlite3", "allowed_files": ["orders/repository.py"]}, actor="test")
    assert c["status"] == "statically_verified"
    return repo, st, c["id"]


@pytest.fixture
def ref_orders(tmp_path):
    """A reference implementation that makes different assumptions (lock, timeout, env var); tag v1."""
    root = tmp_path / "ref_orders"
    (root / "orders").mkdir(parents=True)
    (root / "orders" / "__init__.py").write_text("", encoding="utf-8")
    (root / "orders" / "repository.py").write_text(REF_REPOSITORY, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "repository: serialize writes, busy timeout")
    _git(root, "tag", "v1")
    return root, _git(root, "rev-parse", "HEAD")


def _history(st, cid):
    return [(h["from_status"], h["to_status"], h["reason"]) for h in st.history(cid)]


STEP_NAMES = ["prior_claim", "inspect_reference", "trace_mechanism", "assumptions", "why", "compare",
              "rechallenge", "propositions", "decide", "apply"]


# (a) wrong critique -> confirmed -----------------------------------------------------------------

def test_wrong_critique_is_recorded_but_claim_confirmed(orders):
    repo, st, cid = orders
    before = Claims(st, repo).get(cid)
    hist_before = _history(st, cid)
    fb = feedback.add(st, repo, "orders/api.py also opens sqlite3 connections itself", claim_id=cid,
                      expect_pattern=r"sqlite3", expect_in="orders/api.py")
    assert fb["status"] == "open" and fb["verdict"] is None
    shown = Claims(st, repo).show(cid)
    assert [q["type"] for q in shown["qualifying"]] == ["user_feedback"]  # qualifies, never supports/refutes
    assert shown["status"] == before["status"]  # adding feedback changes nothing
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "confirmed", res["explanation"]
    assert "not supported" in res["explanation"] and "does not hold locally" in res["explanation"]
    after = Claims(st, repo).get(cid)
    assert after["status"] == before["status"] and after["confidence"] == pytest.approx(before["confidence"])
    hist_after = _history(st, cid)
    assert hist_after[: len(hist_before)] == hist_before  # history preserved, append-only
    assert any("feedback" in r and "confirmed" in r for _, _, r in hist_after[len(hist_before):])
    row = st.get("feedback", fb["id"])
    assert row["status"] == "processed" and row["verdict"] == "confirmed"
    run = row["resolution"]["runs"][0]
    assert [s["name"] for s in run["steps"]] == STEP_NAMES
    props = next(s for s in run["steps"] if s["name"] == "propositions")["result"]
    assert props["local"]["holds"] is False and props["local"]["files_scanned"] == 1
    assert run["topic"] == "sqlite3 repository"
    shown = Claims(st, repo).show(cid)
    assert not shown["refuting"]


# (b) correct critique -> corrected (+ supersede with correction) -------------------------------------

def test_valid_critique_with_correction_supersedes(orders):
    repo, st, cid = orders
    hist_before = _history(st, cid)
    (repo / "orders" / "cache.py").write_text(CACHE, encoding="utf-8", newline="\n")
    fb = feedback.add(st, repo, "orders/cache.py talks to sqlite3 as well", claim_id=cid,
                      correction="orders/repository.py and orders/cache.py call sqlite3",
                      expect_pattern="sqlite3", expect_in="orders/*.py")
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "corrected", res["explanation"]
    assert "at least as strong" in res["explanation"]
    cl = Claims(st, repo)
    old = cl.show(cid)
    new_id = res["superseding_claim"]["id"]
    assert old["status"] == "contradicted" and old["superseded_by"] == new_id
    assert any(e["at"] == "orders/cache.py:2" for e in old["refuting"])
    refuting_locs = [e["at"] for e in old["refuting"]]
    assert len(refuting_locs) == len(set(refuting_locs))  # evidence rows are reused, not duplicated
    assert [h["created_at"] for h in old["history"]][: len(hist_before)] and \
        _history(st, cid)[: len(hist_before)] == hist_before
    assert old["history"][-1]["to_status"] == "contradicted"
    new = cl.show(new_id)
    assert new["supersedes"] == cid and new["text"] == "orders/repository.py and orders/cache.py call sqlite3"
    assert new["status"] == "statically_verified" and new["kind"] == "exclusive"
    assert new["spec"]["allowed_files"] == ["orders/repository.py", "orders/cache.py"]
    assert new["spec"]["from_feedback"] == fb["id"]
    assert {e["at"] for e in new["supporting"]} >= {"orders/repository.py:3", "orders/cache.py:2"}
    # old claim still retrievable with full history; nothing deleted
    assert st.claim(cid) is not None and len(st.history(cid)) > len(hist_before)
    row = st.get("feedback", fb["id"])
    assert row["status"] == "processed" and row["verdict"] == "corrected"
    # the corrected claim survives its own critique
    from verinoda import critique

    ch = critique.challenge(st, repo, new_id)
    assert not [f for f in ch["findings"] if f["result"] == "fail"], ch["findings"]


def test_valid_critique_without_correction_contradicts_and_leaves_statement_unknown(orders):
    repo, st, cid = orders
    (repo / "orders" / "cache.py").write_text(CACHE, encoding="utf-8", newline="\n")
    fb = feedback.add(st, repo, "sqlite3 is used outside the repository", claim_id=cid)
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "corrected"
    assert res["superseding_claim"] is None
    assert res["applied"]["corrected_statement"] == "unknown"
    c = Claims(st, repo).get(cid)
    assert c["status"] == "contradicted" and c["superseded_by"] is None


# (c) differing reference -> qualified ------------------------------------------------------------------

def test_reference_with_different_assumptions_qualifies(orders, ref_orders):
    repo, st, cid = orders
    ref_root, ref_sha = ref_orders
    before = Claims(st, repo).get(cid)
    fb = feedback.add(st, repo, "The reference implementation serializes writes with a lock; the claim ignores that",
                      claim_id=cid, reference=str(ref_root), ref="v1")
    res = feedback.process(st, repo, fb["id"], topic="order repository sqlite")
    assert res["verdict"] == "qualified", res["explanation"]
    assert "threading.Lock" in res["explanation"]
    step2 = next(s for s in res["steps"] if s["name"] == "inspect_reference")["result"]
    assert step2["status"] == "ok" and step2["resolved_commit"] == ref_sha and step2["resolved_tag"] == "v1"
    cl = Claims(st, repo)
    c = cl.show(cid)
    assert c["status"] == before["status"]  # holds locally
    assert any(u.startswith(f"validity bounded (feedback {fb['id']})") for u in c["uncertainties"])
    q = [st.evidence(e["id"]) for e in c["qualifying"] if e["type"] == "reference_repo"]
    assert q and all(e["commit_sha"] == ref_sha and e["meta"]["root"] == step2["checkout"] for e in q)
    cmp_step = next(s for s in res["steps"] if s["name"] == "compare")["result"]
    assert ("threading.Lock", ["orders/repository.py:6"]) in [tuple(x) for x in cmp_step["categories"]["concurrency"]["only_reference"]]
    # re-processing the same feedback (topic untraceable locally) -> unresolved; its own earlier bound is
    # retracted from the claim's uncertainties, while history keeps both the bound and the retraction
    n_hist = len(st.history(cid))
    again = feedback.process(st, repo, fb["id"], topic="busy timeout lock")
    assert again["verdict"] == "unresolved" and again["applied"].get("retracted_earlier_bound") is True
    c2 = cl.show(cid)
    assert not any(u.startswith(f"validity bounded (feedback {fb['id']})") for u in c2["uncertainties"])
    reasons = [h["reason"] for h in c2["history"]]
    assert len(reasons) > n_hist and any("earlier validity bound is retracted" in r for r in reasons)
    assert any(r.startswith("qualified: validity bounded") for r in reasons)
    row = st.get("feedback", fb["id"])
    assert [r["verdict"] for r in row["resolution"]["runs"]] == ["qualified", "unresolved"]


def test_topic_untraceable_on_one_side_is_unresolved_not_qualified(orders, ref_orders):
    repo, st, cid = orders
    ref_root, _ = ref_orders
    fb = feedback.add(st, repo, "writes must hold a lock with a busy timeout", claim_id=cid, reference=str(ref_root))
    res = feedback.process(st, repo, fb["id"], topic="busy timeout lock")
    trace = next(s for s in res["steps"] if s["name"] == "trace_mechanism")["result"]
    assert not trace["local"]["seeds"] and trace["reference"]["seeds"]  # precondition of the scenario
    assert res["verdict"] == "unresolved", res["explanation"]
    assert "matched no code on the local side" in res["explanation"]
    assert any('--topic "sqlite3 repository"' in s for s in res["next_steps"])
    assert Claims(st, repo).get(cid)["status"] == "statically_verified"


def test_identical_reference_confirms(orders, tmp_path):
    repo, st, cid = orders
    twin = tmp_path / "twin"
    shutil.copytree(EXAMPLE, twin, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.db"))
    _git(twin, "init", "-q")
    _git(twin, "add", "-A")
    _git(twin, "commit", "-qm", "twin")
    fb = feedback.add(st, repo, "upstream does persistence differently", claim_id=cid, reference=str(twin))
    res = feedback.process(st, repo, fb["id"], topic="order repository sqlite")
    assert res["verdict"] == "confirmed", res["explanation"]
    assert "no differing assumptions" in res["explanation"]
    assert any("pinned default-branch HEAD" in (s["result"].get("pin") or "")
               for s in res["steps"] if s["name"] == "inspect_reference")


def test_decide_ignores_trivial_differences():
    claim = {"id": "clm_x", "status": "statically_verified", "kind": "general"}
    evs = [{"relation": "supports", "source_type": "source_code", "locator": "a.py:1"}]
    crit = {"findings": [{"check": "support", "result": "pass", "detail": ""}]}
    cat = {"shared": [], "differing": [], "counts": {},
           "only_local": [{"key": "container dict literal", "at": ["a.py:3"], "evidence": []}],
           "only_reference": [{"key": "param type str", "at": ["b.py:4"], "evidence": []}]}
    ctx = {"fid": "fbk_x", "topic": "t", "claim": claim, "evidence": evs, "critique": crit,
           "reference": {"status": "ok", "kind": "git", "reference": "r", "resolved_commit": "abc"},
           "compare": {"status": "ok", "categories": {"data_structures": cat},
                       "local": {"seeds": ["f()"]}, "reference": {"seeds": ["g()"]}},
           "props": {}, "repo": Path(".")}
    verdict, why, _, details = feedback.decide(ctx)
    assert verdict == "confirmed" and details["trivial_differences_ignored"] == 2 and "2 trivial" in why
    cat["only_reference"].append({"key": "dataclass Order", "at": ["b.py:9"], "evidence": ["evd_1"]})
    verdict, why, _, details = feedback.decide(ctx)
    assert verdict == "qualified" and "dataclass Order" in why and details["qualifying_evidence"] == ["evd_1"]
    # a reference that was inspected but could not be compared never yields confirmed/qualified
    failed = {**ctx, "compare": None, "compare_error": "RuntimeError: index build failed"}
    verdict, why, steps, _ = feedback.decide(failed)
    assert verdict == "unresolved" and "index build failed" in why and steps


# (d) unreachable reference -> unresolved -------------------------------------------------------------------

def test_unreachable_reference_is_unresolved_with_next_steps(orders):
    repo, st, cid = orders
    before = Claims(st, repo).get(cid)
    fb = feedback.add(st, repo, "upstream does this differently", claim_id=cid, reference=UNREACHABLE_REPO, ref="v2")
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "unresolved"
    assert "could not be inspected" in res["explanation"] and "unreachable" in res["explanation"]
    assert res["next_steps"] and any("feedback process" in s for s in res["next_steps"])
    assert Claims(st, repo).get(cid)["status"] == before["status"]
    row = st.get("feedback", fb["id"])
    assert row["status"] == "processed" and row["verdict"] == "unresolved"
    assert row["resolution"]["latest"]["next_steps"] == res["next_steps"]


def test_invalid_proposition_is_reported_not_evaluated(orders):
    repo, st, cid = orders
    fb = feedback.add(st, repo, "api.py uses sqlite", claim_id=cid, expect_pattern="sqlite3(", expect_in="orders/*.py")
    assert fb["warnings"] and "not a valid regex" in fb["warnings"][0]
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "confirmed"
    assert any("proposition was not evaluated" in s for s in res["next_steps"])


def test_feedback_without_claim_is_unresolved(orders):
    repo, st, _ = orders
    fb = feedback.add(st, repo, "pricing ignores currency")
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "unresolved" and "not linked to a claim" in res["explanation"]
    assert res["next_steps"]


# (e) manual resolution rules ---------------------------------------------------------------------------------

def test_resolve_requires_evidence_and_applies_claim_updates(orders):
    repo, st, cid = orders
    fb = feedback.add(st, repo, "cache.py uses sqlite3", claim_id=cid)
    user_ev = feedback.show(st, fb["id"])["user_evidence_id"]
    hist = _history(st, cid)
    r = feedback.resolve(st, repo, fb["id"], "corrected", reason="looked at it", evidence_ids=[])
    assert r["status"] == "rejected" and "requires at least one existing evidence" in r["error"]
    r = feedback.resolve(st, repo, fb["id"], "corrected", reason="x", evidence_ids=["evd_doesnotexist"])
    assert r["status"] == "rejected" and "does not exist" in r["error"]
    r = feedback.resolve(st, repo, fb["id"], "confirmed", reason="x", evidence_ids=[user_ev])
    assert r["status"] == "rejected"
    r = feedback.resolve(st, repo, fb["id"], "maybe", reason="x", evidence_ids=[])
    assert r["status"] == "rejected"
    # nothing changed by the rejections
    row = st.get("feedback", fb["id"])
    assert row["status"] == "open" and row["verdict"] is None and _history(st, cid) == hist
    # a valid manual correction applies the same updates as process()
    (repo / "orders" / "cache.py").write_text(CACHE, encoding="utf-8", newline="\n")
    snap = workflow.update(st, repo)["snapshot"]
    ev = evmod.source_evidence(repo, "orders/cache.py", 2, commit=snap["commit_sha"])
    eid = evmod.add(st, ev)
    r = feedback.resolve(st, repo, fb["id"], "corrected", reason="orders/cache.py:2 imports sqlite3",
                         evidence_ids=[eid], correction="orders/repository.py and orders/cache.py call sqlite3")
    assert r["status"] == "resolved" and r["verdict"] == "corrected"
    new_id = r["superseding_claim"]["id"]
    old = Claims(st, repo).get(cid)
    assert old["status"] == "contradicted" and old["superseded_by"] == new_id
    assert Claims(st, repo).get(new_id)["status"] == "statically_verified"
    row = st.get("feedback", fb["id"])
    assert row["status"] == "resolved" and row["verdict"] == "corrected"
    assert [x["action"] for x in row["resolution"]["log"]] == ["added", "resolved"]


def test_resolve_qualified_and_unresolved(orders):
    repo, st, cid = orders
    fb = feedback.add(st, repo, "only true for a single process", claim_id=cid)
    unres = feedback.resolve(st, repo, fb["id"], "unresolved", reason="need to check deployment", evidence_ids=[])
    assert unres["status"] == "resolved" and unres["verdict"] == "unresolved"
    ev = evmod.source_evidence(repo, "orders/api.py", 6, commit=None)  # `_repo = None` module global
    eid = evmod.add(st, ev)
    # evidence that was never linked to the claim nor produced by this feedback's protocol is foreign
    r = feedback.resolve(st, repo, fb["id"], "qualified", reason="api.py keeps a process-global repository",
                         evidence_ids=[eid])
    assert r["status"] == "rejected" and "neither linked to claim" in r["error"]
    Claims(st, repo).attach(cid, eid, "qualifies", note="reviewer: process-global repository")
    r = feedback.resolve(st, repo, fb["id"], "qualified", reason="api.py keeps a process-global repository",
                         evidence_ids=[eid])
    assert r["status"] == "resolved"
    c = Claims(st, repo).show(cid)
    assert c["status"] == "statically_verified"
    assert any(e["id"] == eid for e in c["qualifying"])
    assert any("process-global" in u for u in c["uncertainties"])
    log = st.get("feedback", fb["id"])["resolution"]["log"]
    assert [x["verdict"] for x in log if x["action"] == "resolved"] == ["unresolved", "qualified"]


# show / list / render / CLI ------------------------------------------------------------------------------------

def test_show_list_render_and_cli(orders, capsys):
    from verinoda import cli

    repo, st, cid = orders
    fb = feedback.add(st, repo, "api.py uses sqlite3", claim_id=cid, expect_pattern="sqlite3", expect_in="orders/api.py")
    feedback.process(st, repo, fb["id"])
    shown = feedback.show(st, fb["id"])
    assert shown["latest"]["verdict"] == "confirmed" and shown["runs"] == 1
    assert [s["name"] for s in shown["latest"]["steps"]] == STEP_NAMES
    lst = feedback.list_feedback(st)
    assert lst[0]["id"] == fb["id"] and lst[0]["verdict"] == "confirmed"
    feedback.render(shown)
    feedback.render(lst)
    feedback.render(feedback.resolve(st, repo, fb["id"], "corrected", reason="x", evidence_ids=[]))
    out = capsys.readouterr().out
    assert "confirmed" in out and "REJECTED" in out and len(out.splitlines()) < 60
    with pytest.raises(KeyError):
        feedback.show(st, "fbk_missing")
    with pytest.raises(KeyError):
        feedback.add(st, repo, "x", claim_id="clm_missing")
    # CLI adapter: add --process in one call
    st.close()
    rc = cli.main(["feedback", "add", "--repo", str(repo), "--text", "repository.py is not the only sqlite3 user",
                   "--claim", cid, "--expect-pattern", "sqlite3", "--expect-in", "orders/service.py",
                   "--process", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["verdict"] == "confirmed" and out["status"] == "processed"
    rc = cli.main(["feedback", "list", "--repo", str(repo)])
    assert rc == 0 and out["id"] in capsys.readouterr().out


# (f) references resolved first (docs/DESIGN.md D16) ------------------------------------------------------------

def _helpers():
    import importlib.util

    p = Path(__file__).resolve().parent / "fixtures" / "references" / "helpers.py"
    spec = importlib.util.spec_from_file_location("references_helpers", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gh_requests(tmp_path, monkeypatch):
    """https://github.com/psf/requests served by a local fixture through git's url.insteadOf (no network)."""
    h = _helpers()
    root = tmp_path / "gh_requests"
    shas = h.requests_repo(root)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{root.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/psf/requests")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")  # anything not rewritten fails instead of going online
    return root, shas


def test_text_version_beats_branch_link_and_the_mismatch_is_reported(orders, gh_requests):
    repo, st, cid = orders
    _, shas = gh_requests
    fb = feedback.add(st, repo, "In requests 2.31 https://github.com/psf/requests/blob/main/requests/sessions.py "
                                "redirects differently", claim_id=cid)
    res = feedback.process(st, repo, fb["id"], topic="order repository sqlite")
    step2 = next(s for s in res["steps"] if s["name"] == "inspect_reference")["result"]
    assert step2["resolved_commit"] == shas["v2.31.0"] and step2["resolved_tag"] == "v2.31.0"
    assert step2["researched"][0]["basis"] == "explicit_text_version"
    assert any(m.startswith("M1 ") for r in step2["references"] for m in r.get("mismatches", []))
    assert step2["resolved_commit"] != shas["main"]  # never the branch the link happened to show


def test_path_missing_at_the_named_version_makes_the_verdict_unresolved(orders, gh_requests):
    repo, st, cid = orders
    before = Claims(st, repo).get(cid)
    fb = feedback.add(st, repo, "requests'in 2.31 sürümünde "
                                "https://github.com/psf/requests/blob/main/src/requests/sessions.py#L3-L5 farklı",
                      claim_id=cid)
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "unresolved", res["explanation"]
    assert "M1b" in res["explanation"] and "src/requests/sessions.py" in res["explanation"]
    assert any("requests/sessions.py" in s for s in res["next_steps"])
    assert Claims(st, repo).get(cid)["status"] == before["status"]
    assert next(s for s in res["steps"] if s["name"] == "compare")["result"]["skipped"].startswith("reference not")


def test_relative_version_asks_the_user_and_stays_unresolved(orders, gh_requests):
    repo, st, cid = orders
    fb = feedback.add(st, repo, "https://github.com/psf/requests eski sürümde böyle değildi", claim_id=cid)
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "unresolved"
    assert "ambiguous" in res["explanation"]
    assert any(s.startswith("ask the user:") for s in res["next_steps"])


def test_offline_and_uncached_reference_is_unresolved(orders, gh_requests):
    repo, st, cid = orders
    fb = feedback.add(st, repo, "upstream is different", claim_id=cid, references=["https://github.com/psf/requests"])
    assert fb["warnings"] and "offline" in " ".join(fb["warnings"])
    res = feedback.process(st, repo, fb["id"], network="off")
    assert res["verdict"] == "unresolved"
    assert "offline" in res["explanation"] and any("--network" in s for s in res["next_steps"])


def test_floating_pin_is_disclosed_in_a_confirmed_verdict(orders, tmp_path):
    repo, st, cid = orders
    twin = tmp_path / "twin2"
    shutil.copytree(EXAMPLE, twin, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.db"))
    _git(twin, "init", "-q")
    _git(twin, "add", "-A")
    _git(twin, "commit", "-qm", "twin")
    fb = feedback.add(st, repo, "upstream does persistence differently", claim_id=cid, references=[str(twin)])
    res = feedback.process(st, repo, fb["id"], topic="order repository sqlite")
    assert res["verdict"] == "confirmed", res["explanation"]
    assert "pinned to default branch" in res["explanation"] and "the user named no version" in res["explanation"]
    decide_step = next(s for s in res["steps"] if s["name"] == "decide")["result"]
    assert decide_step["details"]["floating_pins"][0]["basis"] == "default"


def test_locked_dependency_becomes_dependency_source_evidence(orders, ref_orders):
    repo, st, cid = orders
    ref_root, ref_sha = ref_orders
    (repo / "uv.lock").write_bytes((
        'version = 1\n\n[[package]]\nname = "ref-orders"\nversion = "1"\n'
        f'source = {{ git = "git+{ref_root.as_uri()}?rev=v1#{ref_sha}" }}\n').encode("utf-8"))
    fb = feedback.add(st, repo, "The reference serializes writes with a lock", claim_id=cid,
                      reference=str(ref_root), ref="v1")
    res = feedback.process(st, repo, fb["id"], topic="order repository sqlite")
    step2 = next(s for s in res["steps"] if s["name"] == "inspect_reference")["result"]
    assert step2["source_type"] == "dependency_source" and step2["resolved_commit"] == ref_sha
    run = st.get("feedback", fb["id"])["resolution"]["runs"][-1]
    types = {st.evidence(e)["source_type"] for e in run["evidence_ids"]}
    assert "dependency_source" in types and "reference_repo" not in types
    assert evmod.SOURCE_RANK["dependency_source"] == 4


# (g) acceptance-audit defects -------------------------------------------------------------------------------------

def test_confirmed_never_uses_foreign_evidence_or_raises_a_claim(orders):
    repo, st, _ = orders
    snap = st.latest_snapshot()
    weak = Claims(st, repo).create("Orders are stored in PostgreSQL", project=snap["project"], snapshot=snap,
                                   subjects=["orders/repository.py"], status="unknown", evidence=[], actor="test")
    before = Claims(st, repo).get(weak["id"])
    fb = feedback.add(st, repo, "that is wrong, it is sqlite", claim_id=weak["id"])
    unrelated = evmod.add(st, evmod.source_evidence(repo, "orders/pricing.py", 1, commit=snap["commit_sha"]))
    r = feedback.resolve(st, repo, fb["id"], "confirmed", reason="looks fine", evidence_ids=[unrelated])
    assert r["status"] == "rejected" and "foreign evidence" in r["error"]
    after = Claims(st, repo).get(weak["id"])
    assert (after["status"], after["confidence"]) == (before["status"], before["confidence"])


def test_confirmed_that_cannot_raise_the_claim_says_why(orders):
    """A confirmation never raises a claim; when the cited evidence does not state it, the rule is surfaced."""
    repo, st, _ = orders
    snap = st.latest_snapshot()
    ev = evmod.source_evidence(repo, "orders/repository.py", 3, commit=snap["commit_sha"])  # `import sqlite3`
    weak = Claims(st, repo).create("Orders are stored in PostgreSQL", project=snap["project"], snapshot=snap,
                                   subjects=["orders/repository.py"], status="statically_verified",
                                   evidence=[(ev, "supports")], actor="test")
    before = Claims(st, repo).get(weak["id"])
    assert before["status"] not in ("statically_verified", "observed")  # the line does not state the claim
    linked = next(e["id"] for e in Claims(st, repo).evidence(weak["id"]))
    fb = feedback.add(st, repo, "this is right", claim_id=weak["id"])
    r = feedback.resolve(st, repo, fb["id"], "confirmed", reason="the import is there", evidence_ids=[linked])
    assert r["status"] == "resolved" and r["verdict"] == "confirmed"
    after = Claims(st, repo).get(weak["id"])
    assert after["status"] == before["status"]  # never raised
    note = r["note"]
    assert note == r["applied"]["note"]
    assert note.startswith(f"claim {weak['id']} stays {before['status']}: ")
    assert "statically_verified" in note and "does not state the claim" in note, note
    # a claim already at the level its evidence could give needs no note
    r2 = feedback.resolve(st, repo, feedback.add(st, repo, "right", claim_id=_claim_ok(st, repo))["id"],
                          "confirmed", reason="ok", evidence_ids=[_first_evidence(st, repo)])
    assert r2["status"] == "resolved" and "note" not in r2


def _claim_ok(st, repo):
    return st.one("SELECT id FROM claims WHERE text = 'Only orders/repository.py calls sqlite3'")["id"]


def _first_evidence(st, repo):
    return Claims(st, repo).evidence(_claim_ok(st, repo))[0]["id"]


def test_confirmed_with_own_run_evidence_keeps_the_status(orders):
    repo, st, cid = orders
    fb = feedback.add(st, repo, "repository.py is where sqlite3 lives", claim_id=cid, expect_pattern="sqlite3",
                      expect_in="orders/repository.py")
    feedback.process(st, repo, fb["id"])
    run = st.get("feedback", fb["id"])["resolution"]["runs"][-1]
    assert run["evidence_ids"]  # the proposition hits were produced by this run
    before = Claims(st, repo).get(cid)
    r = feedback.resolve(st, repo, fb["id"], "confirmed", reason="the hits are the claim's own file",
                         evidence_ids=run["evidence_ids"][:1])
    assert r["status"] == "resolved", r
    after = Claims(st, repo).get(cid)
    assert after["status"] == before["status"] and after["confidence"] <= before["confidence"] + 1e-9


def test_proposition_at_changed_cited_lines_reaches_corrected(orders):
    repo, st, _ = orders
    cfg = repo / "orders" / "config.py"
    original = cfg.read_bytes()
    cfg.write_bytes(original.replace(b'"100.0"', b'"250.0"'))
    snap = workflow.update(st, repo)["snapshot"]
    ev = evmod.source_evidence(repo, "orders/config.py", 7, commit=snap["commit_sha"])
    claim = Claims(st, repo).create("Default discount threshold is 250", project=snap["project"], snapshot=snap,
                                    subjects=["orders/config.py"], status="statically_verified",
                                    evidence=[(ev, "supports")], actor="test")
    cfg.write_bytes(original)  # the code says 100.0 again
    fb = feedback.add(st, repo, "the default threshold is 100.0", claim_id=claim["id"],
                      correction="Default discount threshold is 100.0", expect_pattern=r'"100\.0"',
                      expect_in="orders/config.py")
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "corrected", res["explanation"]
    decide_step = next(s for s in res["steps"] if s["name"] == "decide")["result"]
    assert "orders/config.py:7" in decide_step["details"]["refutes_via"]
    old = Claims(st, repo).get(claim["id"])
    assert old["status"] == "contradicted" and old["superseded_by"] == res["superseding_claim"]["id"]
    assert res["superseding_claim"]["text"] == "Default discount threshold is 100.0"


def test_stale_reason_names_the_changed_lines(orders):
    repo, st, cid = orders
    p = repo / "orders" / "repository.py"
    p.write_bytes(p.read_bytes().replace(b"import sqlite3", b"import sqlite3 as db", 1))
    Claims(st, repo).set_status(cid, "stale", reason="verify: cited lines changed", actor="test")
    fb = feedback.add(st, repo, "repository.py changed", claim_id=cid)
    res = feedback.process(st, repo, fb["id"])
    assert res["verdict"] == "unresolved"
    assert "the lines the claim cites no longer match" in res["explanation"]
    assert "files behind the claim changed since its snapshot" not in res["explanation"]


def test_index_refusal_is_surfaced(orders, monkeypatch):
    repo, st, cid = orders
    snap = st.latest_snapshot()
    monkeypatch.setattr(workflow, "update", lambda store, r: {
        "snapshot": snap, "error": "the indexer did not rewrite the graph", "hint": f"verinoda scan {r} --force",
        "stale": [], "mode": "index_refused"})
    fb = feedback.add(st, repo, "api.py uses sqlite3", claim_id=cid, expect_pattern="sqlite3", expect_in="orders/api.py")
    res = feedback.process(st, repo, fb["id"])
    assert any("did not rewrite the graph" in w for w in res["warnings"])
    assert any("--force" in s for s in res["next_steps"])
    run = st.get("feedback", fb["id"])["resolution"]["runs"][-1]
    assert run["local_index"]["error"] and run["local_index"]["hint"]
