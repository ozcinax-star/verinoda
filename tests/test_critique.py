"""Critique: adversarial checks lower confidence, cap status, and contradict on counterexamples.

Runs on git-committed copies of examples/orders_app (never on examples/ itself).
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import critique, index, workflow  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402
from verinoda.claims import CONFIDENCE_CAP, ORDER, Claims  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _copy_example(dst: Path) -> Path:
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                ".pytest_cache", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    return dst


def _line(repo: Path, rel: str, needle: str) -> int:
    return next(i for i, t in enumerate((repo / rel).read_text(encoding="utf-8").splitlines(), 1) if needle in t)


def _rank(status: str) -> int:
    """Lower = stronger; stale/contradicted rank below everything in ORDER."""
    return ORDER.index(status) if status in ORDER else len(ORDER)


@pytest.fixture
def proj(tmp_path):
    repo = _copy_example(tmp_path / "orders_app")
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    yield repo, st, Claims(st, repo), index.load(repo)
    st.close()


def _edge(g, src_label: str, dst_label: str, relation: str = "calls"):
    for u, v, d in g.edges({relation}):
        if g.label(u) == src_label and g.label(v) == dst_label:
            return u, v, d
    raise AssertionError(f"no {src_label} -{relation}-> {dst_label} edge")


def _relation_claim(cl, st, g, u, v, d, *, extra_evidence=(), status="strong_inference"):
    snap = st.latest_snapshot()
    at = f"{d['source_file']}:{d['source_location'][1:]}"
    ev = evmod.graph_edge_evidence({"source": u, "target": v, **d}, graph_path=str(g.path),
                                   commit=snap["commit_sha"])
    return cl.create(f"`{g.label(u)}` {d['relation']} `{g.label(v)}` ({at})", project=snap["project"],
                     snapshot=snap, status=status, evidence=[(ev, "supports"), *extra_evidence],
                     kind="relation", subjects=[f"{g.file(u)}::{g.label(u)}", f"{g.file(v)}::{g.label(v)}"],
                     spec={"source": u, "target": v, "target_label": g.label(v), "relation": d["relation"],
                           "at": at, "confidence": d.get("confidence"), "origin": d.get("_origin")})


def test_inferred_edge_only_claim_is_capped_and_loses_confidence(proj):
    repo, st, cl, g = proj
    u, v, d = _edge(g, "place_order()", ".save()")
    assert d["confidence"] == "INFERRED" and d["_origin"] == index.RECEIVER_ORIGIN
    c = _relation_claim(cl, st, g, u, v, d, status="statically_verified")
    assert c["status"] == "strong_inference"  # the edge alone could not verify it
    res = critique.challenge(st, repo, c["id"], graph=g)
    checks = {f["check"]: f["result"] for f in res["findings"]}
    assert checks["graph_only"] == "warn" and checks["support"] == "warn"
    assert checks["call_site"] == "pass"  # service.py:22 does say `.save(`
    assert res["after"]["status"] == "weak_inference"
    assert res["after"]["confidence"] < res["before"]["confidence"]
    assert res["after"]["confidence"] <= CONFIDENCE_CAP["weak_inference"]
    assert cl.get(c["id"])["spec"]["ceiling"] == "weak_inference"
    assert cl.get(c["id"])["spec"]["assessed"] == "statically_verified"  # the original request, kept apart
    # Critique again with the same findings: same status and confidence (no ratchet).
    again = critique.challenge(st, repo, c["id"], graph=g)
    assert again["after"] == res["after"] and cl.get(c["id"])["spec"]["ceiling"] == "weak_inference"
    # Re-verification cannot lift it back above the lowered ceiling.
    assert workflow.verify(st, repo, c["id"])["after"]["status"] == "weak_inference"


def test_relation_claim_whose_line_does_not_name_target_is_contradicted(proj):
    repo, st, cl, g = proj
    u, _, d = _edge(g, "create_order_handler()", "place_order()")
    wrong_target = next(n for n in g.G.nodes if g.label(n) == "fetch_order()")
    at_line = int(d["source_location"][1:])
    # The cited line is attached as support, but it does not state the claim: it can no longer
    # verify it (criterion 6); critique then finds the definitive counterexample.
    line_ev = evmod.source_evidence(repo, "orders/api.py", at_line, commit=None)
    c = _relation_claim(cl, st, g, u, wrong_target, dict(d), extra_evidence=[(line_ev, "supports")],
                        status="statically_verified")
    assert c["status"] == "strong_inference"
    assert Claims(st, repo).grades(c["id"])[cl.evidence(c["id"])[1]["id"]] == "none"
    res = critique.challenge(st, repo, c["id"], graph=g)
    fail = next(f for f in res["findings"] if f["check"] == "call_site")
    assert fail["result"] == "fail" and "does not mention 'fetch_order'" in fail["detail"]
    # the whole caller body was read too, and the finding states that scope
    assert "no direct call to fetch_order in create_order_handler (orders/api.py:16-21); calls through other " \
           "names are not followed" in fail["detail"]
    assert res["after"]["status"] == "contradicted" and res["refuting_evidence_added"] == 1
    shown = cl.show(c["id"])
    assert [e["at"] for e in shown["refuting"]] == ["orders/api.py:16-21"]  # the caller's body is the counterexample
    assert shown["history"][-1]["actor"] == "critique" and "counterexample" in shown["history"][-1]["reason"]
    assert res["after"]["confidence"] <= res["before"]["confidence"]


def test_off_by_one_citation_of_a_real_call_is_a_warning_not_a_refutation(proj):
    repo, st, cl, g = proj
    u, v, d = _edge(g, "create_order_handler()", "place_order()")
    real = int(d["source_location"][1:])
    c = _relation_claim(cl, st, g, u, v, {**d, "source_location": f"L{real - 1}"})  # the `try:` line above
    res = critique.challenge(st, repo, c["id"], graph=g)
    site = next(f for f in res["findings"] if f["check"] == "call_site")
    assert site["result"] == "warn" and f"calls place_order at orders/api.py:{real}" in site["detail"]
    assert res["after"]["status"] != "contradicted" and res["refuting_evidence_added"] == 0


def _written(cl, st, text, kind, sources, **spec):
    """A claim as `claim add` records it (free text, the cited lines as support)."""
    snap = st.latest_snapshot()
    evs = []
    for s in sources:
        path, _, rng = s.rpartition(":")
        a, _, b = rng.partition("-")
        evs.append((evmod.source_evidence(cl.repo, path, int(a), int(b or a), commit=snap["commit_sha"]), "supports"))
    spec = {"free_text": True, **spec}
    if kind == "relation":
        spec["at"] = sources[0].split("-")[0]
    return cl.create(text, project=snap["project"], snapshot=snap, status="statically_verified", evidence=evs,
                     subjects=[sources[0].rpartition(":")[0]], kind=kind, spec=spec, actor="user")


def test_definitive_misses_are_contradicted_at_creation_with_their_scope(proj):
    repo, st, cl, g = proj
    rel = _written(cl, st, "create_order_handler calls save", "relation", ["orders/api.py:18"], target_label="save",
                   symbol="save")
    res = critique.check_at_creation(st, repo, rel["id"], graph=g)
    assert res["status"] == "contradicted"
    assert "no direct call to save in create_order_handler (orders/api.py:16-21); calls through other names are " \
           "not followed" in res["findings"][0]
    assert cl.show(rel["id"])["history"][-1]["reason"].startswith("checked at creation: ")
    # the reversed direction: the text's caller is OrderRepository.save, whose body is in another file
    rev = _written(cl, st, "OrderRepository.save calls place_order", "relation", ["orders/service.py:22"],
                   target_label="place_order", symbol="place_order")
    assert critique.check_at_creation(st, repo, rev["id"], graph=g)["status"] == "contradicted"
    assert [e["at"] for e in cl.show(rev["id"])["refuting"]] == ["orders/repository.py:15-20"]
    # a config text about another setting than the read it cites
    cfg = _written(cl, st, "The discount threshold is read from ORDERS_MAX_ITEMS", "config", ["orders/config.py:6"],
                   env="ORDERS_MAX_ITEMS", symbol="ORDERS_MAX_ITEMS")
    assert cfg["status"] == "strong_inference"  # the read exists, the binding does not match: not verified
    res = critique.check_at_creation(st, repo, cfg["id"], graph=g)
    assert res["status"] == "contradicted" and "DISCOUNT_THRESHOLD reads ORDERS_DISCOUNT_THRESHOLD at " \
                                               "orders/config.py:7" in res["findings"][0]
    # an order the function's body does not have
    order = _written(cl, st, "`place_order` calls `save` before `validate_items`", "behaviour",
                     ["orders/service.py:19-22"], proposition="save before validate_items in place_order", holds=True,
                     symbol="place_order")
    res = critique.check_at_creation(st, repo, order["id"], graph=g)
    assert res["status"] == "contradicted" and res["findings"] == [
        "in place_order (orders/service.py:19-22), `validate_items` is first called at line 20, before `save` at line 22"]
    # a definition the file does not have
    loc = _written(cl, st, "`place_orders` is defined in orders/service.py", "location", ["orders/service.py:19-22"],
                   symbol="place_orders")
    res = critique.check_at_creation(st, repo, loc["id"], graph=g)
    assert res["status"] == "contradicted" and res["findings"] == [
        "no definition, assignment or import named `place_orders` in orders/service.py, and the file does not spell "
        "`place_orders` (scope: the file's text); nearest: place_order"]


def test_names_bound_by_assignments_are_defined_and_never_contradicted(proj):
    repo, st, cl, g = proj
    for text, at, sym in (("`DISCOUNT_THRESHOLD` is defined in orders/config.py", "orders/config.py:7",
                           "DISCOUNT_THRESHOLD"),
                          ("`_repo` is defined in orders/api.py", "orders/api.py:6", "_repo"),
                          ("`place_order` is defined in orders/service.py", "orders/service.py:19-22",
                           "orders/service.py::place_order")):
        c = _written(cl, st, text, "location", [at], symbol=sym)
        assert c["status"] == "statically_verified", text
        assert critique.check_at_creation(st, repo, c["id"], graph=g)["findings"] == [], text
    # spelled in the file (an SQL string) but bound nowhere: a heuristic doubt, never a contradiction
    c = _written(cl, st, "`orders` is defined in orders/repository.py", "location", ["orders/repository.py:12"],
                 symbol="orders")
    assert critique.check_at_creation(st, repo, c["id"], graph=g)["status"] != "contradicted"
    ctx = critique.ProbeContext(repo=repo, kind="location", spec={"symbol": "orders", "free_text": True},
                                subjects=["orders/repository.py"], text=c["text"], graph=g)
    [r] = critique.probe_location_exists(ctx)
    assert r.strength == "heuristic" and "spells `orders` but nothing in its syntax tree binds it" in r.detail


def test_relation_sentences_are_contradicted_only_on_roles_they_state_clearly(proj):
    repo, st, cl, g = proj
    true = [("validate_items and compute_total are called in place_order", "orders/service.py:21", "compute_total"),
            ("place_order, validate_items ve compute_total'ı çağırır", "orders/service.py:21", "compute_total"),
            ("create_order_handler, get_repo ile place_order'ı çağırır", "orders/api.py:18", "place_order"),
            ("create_order_handler calls place_order, and get_order_handler calls fetch_order", "orders/api.py:25",
             "fetch_order"),
            ("validate_items'ı place_order çağırır", "orders/service.py:20", "validate_items"),
            ("place_order calls validate_items and compute_total", "orders/service.py:21", "compute_total")]
    for text, at, target in true:
        c = _written(cl, st, text, "relation", [at], target_label=target, symbol=target)
        assert c["status"] == "statically_verified", text
        assert critique.check_at_creation(st, repo, c["id"], graph=g)["findings"] == [], text
    # no clear form: the roles are not bound, so the text is not verified - and never contradicted
    for text, at, target in (("place_order is what create_order_handler calls", "orders/api.py:18", "place_order"),
                             ("place_order'ı çağıran fonksiyon create_order_handler", "orders/api.py:18",
                              "place_order"),
                             ("Both create_order_handler and get_order_handler call get_repo", "orders/api.py:18",
                              "get_repo")):
        c = _written(cl, st, text, "relation", [at], target_label=target, symbol=target)
        assert c["status"] == "strong_inference", text
        assert critique.check_at_creation(st, repo, c["id"], graph=g)["status"] == "strong_inference", text
    # a caller the text names whose definition is not found: its body was not read, so no contradiction
    c = _written(cl, st, "OrderRepository.place_order calls validate_items", "relation", ["orders/service.py:20"],
                 target_label="validate_items", symbol="validate_items")
    res = critique.check_at_creation(st, repo, c["id"], graph=g)
    assert res["status"] != "contradicted" and res["findings"] == []


def test_a_plain_word_caller_is_read_but_never_drives_a_contradiction(proj):
    # review round 2: "checkout calls submit" citing the line above the call was contradicted at creation
    # (the plain-word caller's body was never read), while a caller written as code only got a warning
    repo, st, cl, g = proj
    (repo / "orders" / "extras.py").write_text(
        "from orders.repository import OrderRepository\nfrom orders.service import place_order as submit\n\n\n"
        "def checkout(customer, items):\n    repo = OrderRepository(':memory:')\n"
        "    return submit(repo, customer, items)\n\n\ndef audit(items):\n    return list(items)\n",
        encoding="utf-8", newline="\n")
    for text in ("checkout calls submit", "submit is called by checkout", "checkout, submit'i çağırır"):
        c = _written(cl, st, text, "relation", ["orders/extras.py:6"], target_label="submit", symbol="submit")
        assert critique.check_at_creation(st, repo, c["id"], graph=g)["status"] != "contradicted", text
        cs = critique.call_site_check(repo, cl.get(c["id"]), graph=g)
        assert cs["strength"] == "heuristic" and "but checkout calls submit at orders/extras.py:7" in cs["detail"], text
    # a plain word that names the definition around the cited line is that caller: its whole body
    # without the call refutes the claim, as for a caller written as code
    c = _written(cl, st, "audit calls submit", "relation", ["orders/extras.py:11"], target_label="submit",
                 symbol="submit")
    res = critique.check_at_creation(st, repo, c["id"], graph=g)
    assert res["status"] == "contradicted" and "no direct call to submit in audit (orders/extras.py:10-11)" in \
        res["findings"][0]
    c = _written(cl, st, "checkout calls place_orders", "relation", ["orders/extras.py:7"], target_label="place_orders",
                 symbol="place_orders")
    assert critique.check_at_creation(st, repo, c["id"], graph=g)["status"] == "contradicted"
    # any other plain word is not the caller: a heuristic doubt, never a contradiction (D31)
    c = _written(cl, st, "The handler calls submit", "relation", ["orders/extras.py:6"], target_label="submit",
                 symbol="submit")
    assert critique.check_at_creation(st, repo, c["id"], graph=g)["status"] != "contradicted"
    cs = critique.call_site_check(repo, cl.get(c["id"]), graph=g)
    assert cs["strength"] == "heuristic" and "caller `handler` is a plain word that is not a definition" in cs["detail"]


def test_order_of_calls_made_by_nested_functions_is_never_certain(proj):
    repo, st, cl, g = proj
    (repo / "orders" / "flow.py").write_text(
        "from orders.service import validate_items\n\n\n"
        "def save_items(items):\n    return list(items)\n\n\n"
        "def handle(items):\n    def finish():\n        return save_items(items)\n"
        "    validate_items(items)\n    return finish()\n\n\n"
        "def mixed(items):\n    check = lambda: validate_items(items)  # noqa: E731\n"
        "    save_items(items)\n    validate_items(items)\n    return check()\n", encoding="utf-8", newline="\n")
    for prop in ("validate_items before save_items", "save_items before validate_items"):
        c = _written(cl, st, f"`handle` calls ... ({prop})", "behaviour", ["orders/flow.py:8-12"],
                     proposition=f"{prop} in handle", holds=True, symbol="handle")
        assert c["status"] == "strong_inference", prop  # partial: the nested call has no static place
        assert critique.check_at_creation(st, repo, c["id"], graph=g)["status"] == "strong_inference", prop
    # F's own calls are reversed, but a lambda also calls one of them: a heuristic doubt only
    ctx = critique.ProbeContext(repo=repo, kind="behaviour", subjects=["orders/flow.py"], text="x", graph=g,
                                spec={"proposition": "validate_items before save_items in mixed", "holds": True})
    [r] = critique.probe_order(ctx)
    assert r.strength == "heuristic" and "a nested function or lambda also calls one of them" in r.detail


def test_true_written_claims_keep_their_status_at_creation(proj):
    repo, st, cl, g = proj
    cases = [
        _written(cl, st, "create_order_handler calls place_order", "relation", ["orders/api.py:18"],
                 target_label="place_order", symbol="place_order"),
        _written(cl, st, "validate_items is called by place_order", "relation", ["orders/service.py:20"],
                 target_label="validate_items", symbol="validate_items"),
        _written(cl, st, "The discount threshold is read from ORDERS_DISCOUNT_THRESHOLD", "config",
                 ["orders/config.py:7"], env="ORDERS_DISCOUNT_THRESHOLD", symbol="ORDERS_DISCOUNT_THRESHOLD"),
        _written(cl, st, "`place_order` calls `validate_items` before `save`", "behaviour", ["orders/service.py:19-22"],
                 proposition="validate_items before save in place_order", holds=True, symbol="place_order"),
        _written(cl, st, "`place_order` is defined at orders/service.py:19-22", "location", ["orders/service.py:19-22"],
                 symbol="place_order"),
    ]
    for c in cases:
        assert c["status"] == "statically_verified", c["text"]
        res = critique.check_at_creation(st, repo, c["id"], graph=g)
        assert res == {"claim": c["id"], "status": "statically_verified", "findings": []}, c["text"]
    # the off-by-one citation of a real call: not verified, never contradicted
    off = _written(cl, st, "place_order calls compute_total", "relation", ["orders/service.py:20"],
                   target_label="compute_total", symbol="compute_total")
    assert critique.check_at_creation(st, repo, off["id"], graph=g)["status"] == "weak_inference"


def test_relation_claim_with_matching_call_site_passes(proj):
    repo, st, cl, g = proj
    u, v, d = _edge(g, "create_order_handler()", "place_order()")
    line_ev = evmod.source_evidence(repo, "orders/api.py", int(d["source_location"][1:]), commit=None)
    c = _relation_claim(cl, st, g, u, v, d, extra_evidence=[(line_ev, "supports")], status="statically_verified")
    res = critique.challenge(st, repo, c["id"], graph=g)
    assert {f["check"]: f["result"] for f in res["findings"]}["call_site"] == "pass"
    assert res["after"] == res["before"] == {"status": "statically_verified", "confidence": 0.9}
    assert res["refuting_evidence_added"] == 0


def _exclusive_claim(repo, st, cl):
    snap = st.latest_snapshot()
    ln = _line(repo, "orders/repository.py", "sqlite3.connect")
    ev = evmod.source_evidence(repo, "orders/repository.py", ln, commit=snap["commit_sha"])
    return cl.create("Only orders/repository.py opens a database connection", project=snap["project"],
                     snapshot=snap, status="statically_verified", evidence=[(ev, "supports")], kind="exclusive",
                     subjects=["orders/repository.py"],
                     spec={"pattern": r"sqlite3\.connect\(", "allowed_files": ["orders/repository.py"]})


def test_exclusive_claim_holds_then_is_contradicted_by_another_file(proj):
    repo, st, cl, g = proj
    c = _exclusive_claim(repo, st, cl)
    ok = critique.challenge(st, repo, c["id"], graph=g)
    assert {f["check"]: f["result"] for f in ok["findings"]}["exclusivity"] == "pass"
    assert ok["after"]["status"] == "statically_verified"

    (repo / "orders" / "audit.py").write_text(
        '"""Audit trail."""\nimport sqlite3\n\n\ndef log(msg):\n    conn = sqlite3.connect("audit.db")\n'
        '    conn.execute("INSERT INTO log VALUES (?)", (msg,))\n', encoding="utf-8")
    bad = critique.challenge(st, repo, c["id"], graph=g)
    fail = next(f for f in bad["findings"] if f["check"] == "exclusivity")
    assert fail["result"] == "fail" and "orders/audit.py:6" in fail["detail"]
    assert bad["after"]["status"] == "contradicted" and bad["refuting_evidence_added"] == 1
    ref = cl.show(c["id"])["refuting"]
    assert [e["at"] for e in ref] == ["orders/audit.py:6"] and "sqlite3.connect" in ref[0]["excerpt"]
    assert bad["after"]["confidence"] <= ok["after"]["confidence"]


def test_staleness_check_marks_changed_claims_stale(proj):
    repo, st, cl, g = proj
    c = _exclusive_claim(repo, st, cl)
    p = repo / "orders" / "repository.py"
    p.write_text(p.read_text(encoding="utf-8").replace("sqlite3.connect(url)", "sqlite3.connect(url, timeout=5)"),
                 encoding="utf-8")
    res = critique.challenge(st, repo, c["id"], graph=g)
    checks = {f["check"]: f["result"] for f in res["findings"]}
    assert checks["staleness"] == "fail" and checks["source_recheck"] == "fail"
    assert res["after"]["status"] == "stale" and res["after"]["confidence"] <= res["before"]["confidence"]


def test_ambiguous_inferred_target_is_warned_with_alternatives(proj):
    repo, st, cl, _ = proj
    (repo / "orders" / "audit_log.py").write_text(
        "class AuditLog:\n    def save(self, entry):\n        return entry\n", encoding="utf-8")
    workflow.update(st, repo)
    g = index.load(repo)
    u, v, d = _edge(g, "place_order()", ".save()")
    assert g.file(v) == "orders/repository.py"
    c = _relation_claim(cl, st, g, u, v, d)
    res = critique.challenge(st, repo, c["id"], graph=g)
    amb = next(f for f in res["findings"] if f["check"] == "ambiguity")
    assert amb["result"] == "warn" and any("orders/audit_log.py" in a for a in res["alternatives"])
    assert res["after"]["confidence"] < res["before"]["confidence"]


def test_confidence_and_status_never_increase_in_critique(proj):
    repo, st, cl, g = proj
    snap = st.latest_snapshot()
    a = _line(repo, "orders/pricing.py", "def compute_total")
    src = evmod.source_evidence(repo, "orders/pricing.py", a, a + 2, commit=snap["commit_sha"])

    def mk(text, status, confidence=None, evidence=None):
        return cl.create(text, project=snap["project"], snapshot=snap, status=status, confidence=confidence,
                         evidence=evidence if evidence is not None else [(dict(src), "supports")],
                         subjects=["orders/pricing.py"])

    low = mk("compute_total adds a surcharge?", "weak_inference", confidence=0.2)
    unknown = mk("nobody knows", "unknown", evidence=[])
    stale = mk("compute_total applies the discount", "statically_verified")
    cl.set_status(stale["id"], "stale", reason="file changed then reverted")
    old = mk("compute_total multiplies", "strong_inference")
    cl.supersede(old["id"], "compute_total sums", evidence=[(dict(src), "supports")], status="statically_verified",
                 reason="corrected", actor="user", snapshot=snap)
    u, v, d = _edge(g, "place_order()", ".save()")
    inferred = _relation_claim(cl, st, g, u, v, d)
    ids = [low["id"], unknown["id"], stale["id"], old["id"], inferred["id"]]
    for _ in range(2):  # repeated critique must not ratchet anything up either
        for cid in ids:
            before = cl.get(cid)
            n_hist = len(st.history(cid))
            res = critique.challenge(st, repo, cid, graph=g)
            assert res["after"]["confidence"] <= before["confidence"] + 1e-9, (cid, res)
            assert _rank(res["after"]["status"]) >= _rank(before["status"]), (cid, res)
            # not even transiently: every history entry written by critique is <= before
            for h in st.history(cid)[n_hist:]:
                assert h["to_confidence"] <= before["confidence"] + 1e-9, (cid, h)
                assert _rank(h["to_status"]) >= _rank(before["status"]), (cid, h)
    assert cl.get(low["id"])["confidence"] <= 0.2
    assert cl.get(stale["id"])["status"] == "stale"      # critique never restores; verify does
    assert cl.get(old["id"])["status"] == "contradicted"  # superseded stays contradicted


def test_challenge_unknown_claim_raises_keyerror(proj):
    repo, st, cl, g = proj
    with pytest.raises(KeyError):
        critique.challenge(st, repo, "clm_nope", graph=g)


# -- idempotency, import aliases, cheap staleness ---------------------------------------------

@pytest.fixture
def plain(tmp_path):
    """The example copied without git or a scan: fast, for checks that need no graph."""
    repo = tmp_path / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc",
                                                                 ".pytest_cache", "*.db"))
    st = open_store(repo)
    yield repo, st, Claims(st, repo)
    st.close()


def test_critique_twice_equals_critique_once(plain):
    repo, st, cl = plain
    a = _line(repo, "orders/pricing.py", "def compute_total")
    src = evmod.source_evidence(repo, "orders/pricing.py", a, a + 2, commit=None)
    c = cl.create(f'orders/pricing.py:{a}-{a + 2} contains: subtotal = sum(i["price"] * i["qty"] for i in items)',
                  project="orders_app", snapshot=None, status="statically_verified", evidence=[(src, "supports")],
                  subjects=["orders/pricing.py"])
    assert c["status"] == "statically_verified"
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8").replace('i["price"] * i["qty"]', 'i["price"] + i["qty"]'),
                 encoding="utf-8")
    once = critique.challenge(st, repo, c["id"])
    assert {f["check"]: f["result"] for f in once["findings"]}["source_recheck"] == "fail"
    n_hist = len(st.history(c["id"]))
    spec_once = cl.get(c["id"])["spec"]
    for _ in range(3):
        again = critique.challenge(st, repo, c["id"])
        assert again["after"] == once["after"], (once, again)
    # One step below the assessed level (as far as the changed evidence allows), confidence
    # capped by that status minus the penalty - and not lowered again by the re-runs.
    assert once["after"] == {"status": "weak_inference",
                             "confidence": pytest.approx(CONFIDENCE_CAP["weak_inference"] - 0.2)}
    assert len(st.history(c["id"])) == n_hist  # the re-runs wrote nothing
    assert cl.get(c["id"])["spec"] == spec_once
    # the quote no longer matches the edited line: the evidence allows strong_inference, one step below is the ceiling
    assert spec_once["assessed"] == "statically_verified" and spec_once["ceiling"] == "weak_inference"


def test_repeated_counterexample_is_linked_once(proj):
    repo, st, cl, g = proj
    u, _, d = _edge(g, "create_order_handler()", "place_order()")
    wrong_target = next(n for n in g.G.nodes if g.label(n) == "fetch_order()")
    c = _relation_claim(cl, st, g, u, wrong_target, dict(d))
    first = critique.challenge(st, repo, c["id"], graph=g)
    second = critique.challenge(st, repo, c["id"], graph=g)
    assert first["after"] == second["after"] and first["after"]["status"] == "contradicted"
    assert (first["refuting_evidence_added"], second["refuting_evidence_added"]) == (1, 0)
    assert len(cl.show(c["id"])["refuting"]) == 1


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).write_text(text, encoding="utf-8")


def _alias_claim(cl, repo: Path, at: str, target_label: str):
    path, _, ln = at.rpartition(":")
    ev = evmod.source_evidence(repo, path, int(ln), commit=None)
    return cl.create(f"{path} calls {target_label} ({at})", project="orders_app", snapshot=None,
                     status="statically_verified", evidence=[(ev, "supports")], kind="relation",
                     subjects=[path], spec={"target_label": target_label, "at": at, "confidence": "EXTRACTED"})


def test_call_through_an_import_alias_is_not_contradicted(plain):
    repo, st, cl = plain
    _write(repo, "orders/alias_use.py",
           "from orders.service import place_order as _place_order\n"
           "import orders.pricing as pricing_mod\n\n\n"
           "def via_alias(items):\n"
           "    return _place_order(items)\n\n\n"
           "def via_module(items):\n"
           "    return pricing_mod.compute_total(items)\n\n\n"
           "def neither(items):\n"
           "    return len(items)\n")
    aliased = _alias_claim(cl, repo, "orders/alias_use.py:6", "place_order()")
    res = critique.challenge(st, repo, aliased["id"])
    site = next(f for f in res["findings"] if f["check"] == "call_site")
    assert site["result"] == "pass" and "_place_order" in site["detail"]
    assert res["after"] == res["before"] and res["refuting_evidence_added"] == 0
    assert cl.show(aliased["id"])["refuting"] == []
    dotted = _alias_claim(cl, repo, "orders/alias_use.py:10", "orders.pricing.compute_total()")
    assert {f["check"]: f["result"] for f in critique.challenge(st, repo, dotted["id"])["findings"]}[
        "call_site"] == "pass"
    # Control: a line that names neither the target nor an alias is still a counterexample.
    wrong = _alias_claim(cl, repo, "orders/alias_use.py:14", "place_order()")
    bad = critique.challenge(st, repo, wrong["id"])
    assert bad["after"]["status"] == "contradicted" and bad["refuting_evidence_added"] == 1


def test_staleness_hashes_only_the_claims_own_files(proj, monkeypatch):
    repo, st, cl, g = proj
    from verinoda import snapshot

    def forbidden(*a, **k):
        raise AssertionError("critique must not hash the whole tree or ask git for status")

    for name in ("current_state", "hash_files", "list_files", "git_info", "git"):
        monkeypatch.setattr(snapshot, name, forbidden)
    snap = st.latest_snapshot()
    a = _line(repo, "orders/pricing.py", "def compute_total")
    ok = cl.create("compute_total exists", project=snap["project"], snapshot=snap, status="statically_verified",
                   evidence=[(evmod.source_evidence(repo, "orders/pricing.py", a, commit=None), "supports")],
                   subjects=["orders/pricing.py"])
    res = critique.challenge(st, repo, ok["id"], graph=g)
    assert {f["check"]: f["result"] for f in res["findings"]}["staleness"] == "pass"
    p = repo / "orders" / "pricing.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")
    res = critique.challenge(st, repo, ok["id"], graph=g)
    stale = next(f for f in res["findings"] if f["check"] == "staleness")
    assert stale["result"] == "fail" and "orders/pricing.py" in stale["detail"]
    assert res["after"]["status"] == "stale"


def test_deleted_claim_file_is_stale_in_critique(proj):
    repo, st, cl, g = proj
    snap = st.latest_snapshot()
    ln = _line(repo, "orders/config.py", "ORDERS_DISCOUNT_THRESHOLD")
    c = cl.create("threshold from env", project=snap["project"], snapshot=snap, status="statically_verified",
                  evidence=[(evmod.source_evidence(repo, "orders/config.py", ln, commit=None), "supports")])
    (repo / "orders" / "config.py").unlink()
    res = critique.challenge(st, repo, c["id"], graph=g)
    stale = next(f for f in res["findings"] if f["check"] == "staleness")
    assert stale["result"] == "fail" and "orders/config.py (removed)" in stale["detail"]
    assert res["after"]["status"] == "stale"


def test_claim_watching_tests_is_stale_in_critique_when_a_test_file_is_added(proj):
    repo, st, cl, g = proj
    snap = st.latest_snapshot()
    c = cl.create("No test statically reaches `compute_total`", project=snap["project"], snapshot=snap,
                  status="weak_inference", kind="tests", subjects=["orders/pricing.py::compute_total"],
                  spec={"watch": "tests"})
    assert {f["check"]: f["result"] for f in critique.challenge(st, repo, c["id"], graph=g)["findings"]}[
        "staleness"] == "pass"
    (repo / "orders" / "notes.py").write_text("X = 1\n", encoding="utf-8")  # not a test file: ignored
    assert {f["check"]: f["result"] for f in critique.challenge(st, repo, c["id"], graph=g)["findings"]}[
        "staleness"] == "pass"
    (repo / "tests" / "test_total_probe.py").write_text("def test_total():\n    assert True\n", encoding="utf-8")
    res = critique.challenge(st, repo, c["id"], graph=g)
    stale = next(f for f in res["findings"] if f["check"] == "staleness")
    assert stale["result"] == "fail" and "tests/test_total_probe.py (added)" in stale["detail"]
    assert res["after"]["status"] == "stale"


# -- refutation strength, probes and the round-3 contracts -------------------------------------

def _plain_relation(cl, repo, text, at, subjects, target_label, **spec):
    path, _, ln = at.rpartition(":")
    ev = evmod.source_evidence(repo, path, int(ln), commit=None)
    return cl.create(text, project="orders_app", snapshot=None, status="strong_inference",
                     evidence=[(ev, "supports")], kind="relation", subjects=subjects,
                     spec={"target_label": target_label, "at": at, "confidence": "EXTRACTED", "relation": "calls",
                           **spec})


def test_benchmark_negative_relations_are_definitively_refuted(plain):
    """orders_app q02.n.handler_save and q07.n.reversed: false relations citing real call lines."""
    repo, st, cl = plain
    handler_save = _plain_relation(cl, repo, "`create_order_handler()` calls `.save()` (orders/api.py:18)",
                                   "orders/api.py:18", ["orders/api.py::create_order_handler()",
                                                        "orders/repository.py::.save()"], ".save()")
    reversed_ = _plain_relation(cl, repo, "`apply_discount()` calls `compute_total()` (orders/pricing.py:8)",
                                "orders/pricing.py:8", ["orders/pricing.py::apply_discount()",
                                                        "orders/pricing.py::compute_total()"], "compute_total()")
    for c in (handler_save, reversed_):
        res = critique.challenge(st, repo, c["id"])
        site = next(f for f in res["findings"] if f["check"] == "call_site")
        assert site["result"] == "fail" and site["strength"] == "definitive", site
        assert res["after"]["status"] == "contradicted"
        ref = cl.evidence(c["id"])[-1]
        assert ref["relation"] == "refutes" and ref["meta"]["strength"] == "definitive"
    assert "not inside `apply_discount`" in next(
        f["detail"] for f in critique.challenge(st, repo, reversed_["id"])["findings"] if f["check"] == "call_site")
    # the true relation on the same line passes
    true = _plain_relation(cl, repo, "`compute_total()` calls `apply_discount()` (orders/pricing.py:8)",
                           "orders/pricing.py:8", ["orders/pricing.py::compute_total()",
                                                   "orders/pricing.py::apply_discount()"], "apply_discount()")
    res = critique.challenge(st, repo, true["id"])
    assert {f["check"]: f["result"] for f in res["findings"]}["call_site"] == "pass"
    assert res["after"]["status"] == "strong_inference"


def test_heuristic_call_site_doubt_lowers_one_step_and_never_contradicts(plain):
    repo, st, cl = plain
    _write(repo, "orders/callbacks.py",
           "from orders.pricing import apply_discount\n\n\n"
           "def discount_all(values):\n"
           "    return sorted(values, key=apply_discount)\n")
    c = _plain_relation(cl, repo, "`discount_all()` calls `apply_discount()` (orders/callbacks.py:5)",
                        "orders/callbacks.py:5", ["orders/callbacks.py::discount_all()",
                                                  "orders/pricing.py::apply_discount()"], "apply_discount()")
    assert c["status"] == "strong_inference"
    res = critique.challenge(st, repo, c["id"])
    site = next(f for f in res["findings"] if f["check"] == "call_site")
    assert site["result"] == "warn" and site["strength"] == "heuristic" and "no call" in site["detail"]
    assert res["after"]["status"] == "weak_inference" and res["refuting_evidence_added"] == 0
    assert any("call site" in u for u in cl.get(c["id"])["uncertainties"])
    assert critique.challenge(st, repo, c["id"])["after"] == res["after"]  # stable


def test_tests_early_exit_probe_qualifies_only_the_doubtful_test(plain):
    """The benchmark's wrong finding (q04.n.empty_order): test_empty_order_rejected never reaches apply_discount."""
    repo, st, cl = plain
    tests = ["tests/test_pricing.py::test_compute_total", "tests/test_service.py::test_place_and_fetch_roundtrip",
             "tests/test_service.py::test_empty_order_rejected"]
    ev = evmod.source_evidence(repo, "tests/test_service.py", 13, commit=None,
                               meta={"test": "test_empty_order_rejected"})
    c = cl.create("Test code statically reaches `apply_discount` from: test_compute_total, "
                  "test_place_and_fetch_roundtrip, test_empty_order_rejected", project="orders_app", snapshot=None,
                  status="strong_inference", evidence=[(ev, "supports")], kind="tests",
                  subjects=["orders/pricing.py::apply_discount()"], spec={"tests": tests})
    assert c["status"] == "strong_inference"
    res = critique.challenge(st, repo, c["id"])
    probes = [f for f in res["findings"] if f["check"] == "tests_early_exit"]
    assert len(probes) == 1 and probes[0]["result"] == "warn" and probes[0]["strength"] == "heuristic"
    d = probes[0]["detail"]
    assert "test_empty_order_rejected" in d and "ValidationError" in d and "orders/service.py:14" in d
    assert "roundtrip" not in d
    assert res["after"]["status"] == "weak_inference" and res["refuting_evidence_added"] == 0
    shown = cl.show(c["id"])
    assert any(q["at"] == "orders/service.py:14" for q in shown["qualifying"])
    assert any("test_empty_order_rejected" in u for u in shown["uncertainties"])


def test_location_span_off_by_n_is_definitively_refuted(plain):
    repo, st, cl = plain
    ev = evmod.source_evidence(repo, "orders/pricing.py", 6, 9, commit=None, meta={"symbol": "compute_total()"})
    c = cl.create("`compute_total()` is defined at orders/pricing.py:6-9", project="orders_app", snapshot=None,
                  status="statically_verified", evidence=[(ev, "supports")], kind="location",
                  subjects=["orders/pricing.py::compute_total()"])
    assert c["status"] == "strong_inference"  # the span does not match the AST: only partial support
    res = critique.challenge(st, repo, c["id"])
    span = next(f for f in res["findings"] if f["check"] == "location_span")
    assert span["result"] == "fail" and span["strength"] == "definitive" and "6-8" in span["detail"]
    assert res["after"]["status"] == "contradicted"
    good_ev = evmod.source_evidence(repo, "orders/pricing.py", 6, 8, commit=None, meta={"symbol": "compute_total()"})
    good = cl.create("`compute_total()` is defined at orders/pricing.py:6-8", project="orders_app", snapshot=None,
                     status="statically_verified", evidence=[(good_ev, "supports")], kind="location",
                     subjects=["orders/pricing.py::compute_total()"])
    assert good["status"] == "statically_verified"
    assert critique.challenge(st, repo, good["id"])["after"]["status"] == "statically_verified"


def test_superseded_decision_record_is_qualified(plain):
    repo, st, cl = plain
    _write(repo, "docs/adr/0002-cache.md", "# ADR 0002: in-memory cache\n\nStatus: superseded\n\n"
                                          "We cache orders in memory.\n")
    ev = evmod.source_evidence(repo, "docs/adr/0002-cache.md", 1, 5, commit=None, source_type="design_doc")
    c = cl.create("Decision record docs/adr/0002-cache.md (status: superseded) explains it: We cache orders in "
                  "memory.", project="orders_app", snapshot=None, status="primary_source_verified",
                  evidence=[(ev, "supports")], kind="decision", subjects=["docs/adr/0002-cache.md"])
    assert c["status"] == "primary_source_verified"  # attributed: the record states it
    res = critique.challenge(st, repo, c["id"])
    d1 = next(f for f in res["findings"] if f["check"] == "decision_status")
    assert d1["result"] == "warn" and "superseded" in d1["detail"]
    assert res["after"]["status"] == "strong_inference"


def test_missing_commit_and_run_are_existence_failures(plain):
    repo, st, cl = plain
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    ghost = {"source_type": "git_history", "locator": "commit " + "ab" * 20, "commit_sha": "ab" * 20,
             "content_hash": "sha256:1", "excerpt": "pricing rules"}
    c = cl.create(f"`compute_total` lines 6-8 were changed in {'ab' * 5}: pricing rules", project="orders_app",
                  snapshot=None, status="primary_source_verified", evidence=[(ghost, "supports")], kind="history",
                  subjects=["orders/pricing.py"])
    res = critique.challenge(st, repo, c["id"])
    ex = next(f for f in res["findings"] if f["check"] == "existence")
    assert ex["result"] == "fail" and "does not exist" in ex["detail"]
    run = {"source_type": "test_result", "locator": "run exp_nope: pytest tests/test_pricing.py",
           "excerpt": "3 passed", "content_hash": "sha256:2", "meta": {"outcome": "pass", "experiment_id": "exp_nope"}}
    r = cl.create("the pricing tests pass", project="orders_app", snapshot=None, status="experiment_verified",
                  evidence=[(run, "supports")])
    res = critique.challenge(st, repo, r["id"])
    assert any(f["check"] == "existence" and "exp_nope" in f["detail"] for f in res["findings"])
    assert res["after"]["status"] not in ("experiment_verified",)


def test_empty_cited_text_fails_source_recheck(plain):
    repo, st, cl = plain
    blank = {"source_type": "source_code", "locator": "orders/api.py:2", "path": "orders/api.py", "line_start": 2,
             "line_end": 2, "content_hash": evmod.content_hash(""), "excerpt": "", "meta": {}}
    c = cl.create("orders api", project="orders_app", snapshot=None, status="statically_verified",
                  evidence=[(blank, "supports")])
    assert c["status"] not in ("statically_verified",)
    res = critique.challenge(st, repo, c["id"])
    assert any(f["check"] == "source_recheck" and "empty" in f["detail"] for f in res["findings"])


def test_challenge_uses_current_files_instead_of_hashing(proj, monkeypatch):
    repo, st, cl, g = proj
    from verinoda import snapshot

    snap = st.latest_snapshot()
    a = _line(repo, "orders/pricing.py", "def compute_total")
    c = cl.create("compute_total exists", project=snap["project"], snapshot=snap, status="statically_verified",
                  evidence=[(evmod.source_evidence(repo, "orders/pricing.py", a, commit=None), "supports")],
                  subjects=["orders/pricing.py"])
    files = st.snapshot_files(snap["id"])
    monkeypatch.setattr(critique, "sha256_file", lambda p: (_ for _ in ()).throw(AssertionError("hashed")))
    for name in ("current_state", "hash_files", "list_files"):
        monkeypatch.setattr(snapshot, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("scanned")))
    res = critique.challenge(st, repo, c["id"], graph=g, current_files=files)
    assert {f["check"]: f["result"] for f in res["findings"]}["staleness"] == "pass"
    changed = {**files, "orders/pricing.py": "0" * 64}
    res = critique.challenge(st, repo, c["id"], graph=g, current_files=changed)
    assert {f["check"]: f["result"] for f in res["findings"]}["staleness"] == "fail"
    assert res["after"]["status"] == "stale"


def test_symbol_level_staleness_ignores_edits_elsewhere_in_the_file(proj):
    repo, st, cl, g = proj
    snap = st.latest_snapshot()
    a = _line(repo, "orders/pricing.py", "def compute_total")
    c = cl.create("`compute_total()` is defined at orders/pricing.py:6-8", project=snap["project"], snapshot=snap,
                  status="statically_verified", kind="location",
                  evidence=[(evmod.source_evidence(repo, "orders/pricing.py", a, a + 2, commit=None,
                                                   meta={"symbol": "compute_total()"}), "supports")],
                  subjects=["orders/pricing.py::compute_total()"])
    assert c["status"] == "statically_verified"
    p = repo / "orders" / "pricing.py"
    p.write_bytes(p.read_bytes().replace(b"return round(subtotal * 0.9, 2)", b"return round(subtotal * 0.8, 2)"))
    res = critique.challenge(st, repo, c["id"], graph=g)
    stale = next(f for f in res["findings"] if f["check"] == "staleness")
    assert stale["result"] == "pass" and "nothing this claim depends on" in stale["detail"]
    assert res["after"]["status"] == "statically_verified"
    p.write_bytes(p.read_bytes().replace(b"def compute_total(items: list[dict])", b"def compute_total(items)"))
    res = critique.challenge(st, repo, c["id"], graph=g)
    assert {f["check"]: f["result"] for f in res["findings"]}["staleness"] == "fail"


def test_verify_does_not_undo_the_critique_penalty(proj):
    repo, st, cl, g = proj
    u, v, d = _edge(g, "place_order()", ".save()")
    c = _relation_claim(cl, st, g, u, v, d)
    res = critique.challenge(st, repo, c["id"], graph=g)
    assert res["after"]["confidence"] < CONFIDENCE_CAP[res["after"]["status"]]
    assert cl.get(c["id"])["spec"]["penalty"] > 0
    v2 = workflow.verify(st, repo, c["id"])
    assert v2["after"]["confidence"] <= res["after"]["confidence"] + 1e-9


def test_flow_claims_check_ambiguity_on_every_inferred_hop(proj):
    repo, st, cl, _ = proj
    (repo / "orders" / "audit_log.py").write_text(
        "class AuditLog:\n    def save(self, entry):\n        return entry\n", encoding="utf-8")
    workflow.update(st, repo)
    g = index.load(repo)
    snap = st.latest_snapshot()
    hops = [{"from": "create_order_handler()", "to": "place_order()", "at": "orders/api.py:18", "relation": "calls",
             "confidence": "EXTRACTED"},
            {"from": "place_order()", "to": ".save()", "at": "orders/service.py:22", "relation": "calls",
             "confidence": "INFERRED"}]
    c = cl.create("Data path create_order_handler() -> place_order() -> .save() reaches persistence",
                  project=snap["project"], snapshot=snap, status="strong_inference", kind="flow",
                  evidence=[(evmod.source_evidence(repo, "orders/api.py", 18, commit=None,
                                                   meta={"hop": "create_order_handler()->place_order()"}), "supports"),
                            (evmod.source_evidence(repo, "orders/service.py", 22, commit=None,
                                                   meta={"hop": "place_order()->.save()"}), "supports")],
                  subjects=["orders/api.py", "orders/repository.py"], spec={"hops": hops})
    res = critique.challenge(st, repo, c["id"], graph=g)
    amb = [f for f in res["findings"] if f["check"] == "ambiguity"]
    assert len(amb) == 1 and amb[0]["result"] == "warn" and "save" in amb[0]["detail"]
    assert any("orders/audit_log.py" in a for a in res["alternatives"])


def test_config_claim_on_a_line_that_reads_another_variable_is_refuted(plain):
    """orders_app q03.n.repo_env: the repository does not read an environment variable itself."""
    repo, st, cl = plain

    def cfg(var, at):
        path, _, ln = at.rpartition(":")
        ev = evmod.source_evidence(repo, path, int(ln), commit=None, meta={"env": var})
        return cl.create(f"`x` in {path} reads environment variable {var} ({at})", project="orders_app",
                         snapshot=None, status="statically_verified", evidence=[(ev, "supports")], kind="config",
                         subjects=[path], spec={"env": var})

    ok = cfg("ORDERS_DATABASE_URL", "orders/config.py:5")
    assert ok["status"] == "statically_verified"
    assert critique.challenge(st, repo, ok["id"])["after"]["status"] == "statically_verified"
    for var, at, words in (("ORDERS_MAX_ITEMS", "orders/config.py:5", "reads ORDERS_DATABASE_URL"),
                           ("ORDERS_DATABASE_URL", "orders/repository.py:9", "no environment read")):
        bad = cfg(var, at)
        assert bad["status"] not in ("statically_verified",)
        res = critique.challenge(st, repo, bad["id"])
        probe = next(f for f in res["findings"] if f["check"] == "config_read")
        assert probe["strength"] == "definitive" and words in probe["detail"]
        assert res["after"]["status"] == "contradicted"


def test_location_claim_with_a_wrong_start_is_refuted(plain):
    repo, st, cl = plain
    ev = evmod.source_evidence(repo, "orders/pricing.py", 5, 8, commit=None, meta={"symbol": "compute_total()"})
    c = cl.create("`compute_total()` is defined at orders/pricing.py:5-8", project="orders_app", snapshot=None,
                  status="statically_verified", evidence=[(ev, "supports")], kind="location",
                  subjects=["orders/pricing.py::compute_total()"])
    res = critique.challenge(st, repo, c["id"])
    assert "6-8" in next(f for f in res["findings"] if f["check"] == "location_span")["detail"]
    assert res["after"]["status"] == "contradicted"


def test_plain_text_number_and_polarity_conflicts_are_heuristic(plain):
    repo, st, cl = plain
    for text, at, words in (("apply_discount multiplies the subtotal by 0.8", "orders/pricing.py:14", "0.9"),
                            ("compute_total does not apply the discount", "orders/pricing.py:6-8", "negation")):
        path, _, rng = at.rpartition(":")
        a, _, b = rng.partition("-")
        ev = evmod.source_evidence(repo, path, int(a), int(b) if b else None, commit=None)
        c = cl.create(text, project="orders_app", snapshot=None, status="statically_verified",
                      evidence=[(ev, "supports")])
        assert c["status"] == "strong_inference"
        res = critique.challenge(st, repo, c["id"])
        warn = next(f for f in res["findings"] if f["check"] == "entailment" and f["result"] == "warn")
        assert words in warn["detail"] and warn["strength"] == "heuristic"
        assert res["after"]["status"] == "weak_inference" and res["refuting_evidence_added"] == 0


def test_critique_evaluation_harness_on_the_labelled_set():
    from verinoda.benchmark import critique_eval

    res = critique_eval.evaluate()
    assert res["cases"] == len(critique_eval.cases()) + len(critique_eval.PLANTED) and res["false"] >= 20
    assert res["presented_as_verified"]["precision"] == 1.0
    assert res["benchmark_negatives"]["stated_as_verified"] == 0
    assert res["benchmark_negatives"]["n"] >= 9
    assert res["critique"]["false_flagged"]["rate"] == 1.0
    assert res["critique"]["true_contradicted"]["k"] == 0
    assert res["gate"]["false_verified_at_creation"]["k"] <= 1  # only the planted "only X" needs critique
    by_id = {r["id"]: r for r in res["rows"]}
    assert by_id["f.reach_empty"]["after"] == "weak_inference" and "tests_early_exit" in by_id["f.reach_empty"]["fired"]
    assert by_id["f.only_repo_planted"]["after"] == "contradicted"
