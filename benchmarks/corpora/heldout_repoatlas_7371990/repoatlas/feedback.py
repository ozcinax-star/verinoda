"""User critique of earlier claims, processed as a hypothesis - never auto-accepted.

``add`` records the critique (text, optional references - ``reference``/``ref``
for one, ``references`` for several -, proposed correction, checkable
proposition ``expect_pattern`` in files ``expect_in``) as ``user_feedback``
evidence linked to the claim with relation ``qualifies``: it is traceable but
never supports or refutes by itself. It also resolves the references offline
(:func:`repoatlas.references.resolve`, docs/DESIGN.md D16), so version
conflicts in the critique are visible immediately.

``process`` runs the verification protocol and stores every step's result in
the feedback row's ``resolution`` JSON:

 1   prior claim and its evidence (``Claims.show``)
 2-3 the references resolved to exact pins (text version > URL tag > local
     version > URL branch > ...; mismatches M1-M12 reported) and inspected at
     those pins (``research_full(pin=...)``); a dependency pinned at the version
     the project locks becomes ``dependency_source`` evidence (rank 4)
 4-6 the mechanism traced (topic given or derived from the claim), its data
     structures / error handling / concurrency / environment assumptions and
     the "why" material (commits, tests, docs) - local and reference
 7   reference vs local assumptions (``research.compare_with``)
 8   the claim re-challenged (``critique.challenge``) and the checkable
     proposition evaluated locally and in the reference checkout; then a
     deterministic verdict (see :func:`decide`)
 9   claim updates go through :class:`repoatlas.claims.Claims` (history is
     append-only, nothing is deleted); the row becomes ``processed``.

Verdicts: ``corrected`` (verifying refuting evidence at least as strong as the
claim's best support - the rule ``claims.check_status`` uses to block verified
statuses), ``qualified`` (holds locally, but the traced reference assumptions
differ), ``confirmed`` (re-check passes, nothing contradicts; the critique is
recorded but unsupported), ``unresolved`` (a reference the critique relies on is
unresolved, ambiguous or conflicting, the reference is unreachable, or the
evidence is insufficient - always with concrete next steps). A confirmed or
qualified verdict that rests on a floating pin (default-branch HEAD, latest)
says so in its explanation.

``resolve`` lets an agent/human set a verdict manually; confirmed/qualified/
corrected need at least one existing, usable evidence id, and the same claim
updates apply (corrected + correction text => ``Claims.supersede``).
``confirmed`` and ``qualified`` accept only evidence already linked to the
claim or produced by this feedback's own protocol runs, and never raise the
claim: foreign evidence cannot vouch for a claim it was never checked against.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath

from repoatlas import evidence as evmod
from repoatlas import research as rs
from repoatlas.claims import ORDER, VERIFIED, ClaimRuleError, Claims
from repoatlas.store import Store, new_id, now

VERDICTS = ("confirmed", "qualified", "corrected", "unresolved")
# References a critique relies on: external ones. Local files and symbols name parts of the project itself;
# they are grounded by the question plan and checked by the claim re-check, never pinned to another version.
NOT_RELIED_CLASSES = {"local_symbol", "local_file"}
BLOCKING_STATUSES = {"ambiguous", "unresolved", "refused"}
MAX_CODE_REFERENCES = 3
MAX_DOC_REFERENCES = 5
NEEDS_EVIDENCE = {"confirmed", "qualified", "corrected"}
UNUSABLE_FOR_VERDICT = {"user_feedback", "search_result", "model_summary"}
_SCAN_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb",
                  ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".swift", ".scala", ".sql", ".toml", ".cfg",
                  ".ini", ".yaml", ".yml", ".json", ".md", ".rst", ".txt", ".sh", ".ps1"}
_EXCLUSIVE_SUFFIXES = (".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php", ".cs")  # as critique.exclusivity
MAX_SCAN_FILES = 3000
# Differences that do not bound a claim's validity by themselves (literal shapes, parameter annotations).
TRIVIAL_FACT = re.compile(r"^(container (dict|list|set) (literal|comprehension)|param type |assert$)")


# =============================================================================
# helpers
# =============================================================================

def _row(store: Store, fid: str) -> dict:
    row = store.get("feedback", fid)
    if row is None:
        raise KeyError(f"no feedback {fid}")
    if not isinstance(row.get("resolution"), dict):
        row["resolution"] = {}
    return row


def _short(text, n: int = 160) -> str:
    text = str(text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 3] + "..."


def _rank(e: dict) -> int:
    return evmod.SOURCE_RANK.get(e["source_type"], 99)


def _claim_brief(c: dict | None) -> dict | None:
    if not c:
        return None
    return {"id": c["id"], "status": c["status"], "confidence": round(float(c["confidence"] or 0), 3),
            "superseded_by": c.get("superseded_by")}


def _derive_topic(claim: dict | None, text: str) -> str:
    """Topic from the claim's pattern, subjects and text (fallback: feedback text)."""
    parts: list[str] = []
    if claim:
        spec = claim.get("spec") or {}
        if spec.get("pattern"):
            parts += re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", str(spec["pattern"]))[:2]
        for key in ("target_label",):
            if spec.get(key):
                parts.append(str(spec[key]).strip(".()").rpartition(".")[2])
        for s in claim.get("subjects") or []:
            s = str(s)
            if "::" in s:
                path, sym = s.split("::", 1)
                parts.append(sym.strip(".()").rpartition(".")[2])
                parts.append(PurePosixPath(path).stem)
            elif "/" in s or PurePosixPath(s).suffix:
                parts.append(PurePosixPath(s).stem)
    if len(parts) < 2:
        from repoatlas.retrieval import terms_for

        parts += terms_for(claim.get("text") or "")[:4] if claim else []
        if not parts:
            parts += terms_for(text)[:4]
    seen: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p.lower() not in {x.lower() for x in seen} and p not in ("__init__", "__main__"):
            seen.append(p)
    return " ".join(seen[:5]) or _short(text, 60)


def _scan(root: Path, rx: re.Pattern, glob: str | None, *, suffixes=None) -> tuple[list[tuple[str, int, str]], int]:
    from repoatlas.snapshot import list_files

    hits: list[tuple[str, int, str]] = []
    n = 0
    for rel in list_files(root)[:MAX_SCAN_FILES]:
        if glob and not (fnmatch.fnmatchcase(rel, glob) or PurePosixPath(rel).match(glob)):
            continue
        if suffixes is not None and PurePosixPath(rel).suffix.lower() not in suffixes:
            continue
        p = root / rel
        try:
            if p.stat().st_size > 2_000_000:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        n += 1
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append((rel, i, line))
    return hits, n


def _eval_proposition(store: Store, root: Path, pattern: str, glob: str | None, *, commit: str | None,
                      external: bool) -> dict:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return {"error": f"invalid regex {pattern!r}: {exc}", "holds": None}
    hits, scanned = _scan(root, rx, glob, suffixes=_SCAN_SUFFIXES)
    out_hits = []
    for rel, i, line in hits[:5]:
        ev = evmod.source_evidence(root, rel, i, commit=commit,
                                   source_type="reference_repo" if external else "source_code",
                                   meta={**({"root": str(root)} if external else {}), "check": "proposition",
                                         "pattern": pattern})
        out_hits.append({"at": f"{rel}:{i}", "path": rel, "line_no": i, "line": _short(line, 120),
                         "evidence_id": rs.record_evidence(store, ev)})
    return {"pattern": pattern, "in": glob, "holds": bool(hits), "hit_count": len(hits), "files_scanned": scanned,
            "hits": out_hits, "root": str(root), "commit": commit}


def _exclusive_correction(store: Store, repo: Path, claim: dict, correction: str, commit: str | None) -> dict:
    """Formalize a correction of an exclusive claim ("only F calls P") and check it.

    The corrected allowed set = files the correction text names that exist.
    The statement is statically verified only if the pattern occurs in every
    named file and nowhere else (same scan as critique's exclusivity check).
    """
    spec = claim.get("spec") or {}
    rx = re.compile(spec["pattern"])
    named = []
    for tok in re.findall(r"[\w./\\-]+\.[A-Za-z0-9]+", correction):
        rel = tok.replace("\\", "/")
        rel = rel[2:] if rel.startswith("./") else rel
        if (repo / rel).is_file() and rel not in named:
            named.append(rel)
    if not named:
        return {"formalized": False, "why": "the correction names no existing file; exclusivity cannot be re-checked"}
    hits, _ = _scan(repo, rx, None, suffixes=set(_EXCLUSIVE_SUFFIXES))
    inside = [h for h in hits if h[0] in named]
    outside = [h for h in hits if h[0] not in named]
    missing = [f for f in named if not any(h[0] == f for h in inside)]
    evs: list[tuple[str, str]] = []
    for f in named:
        for rel, i, _ in [h for h in inside if h[0] == f][:2]:
            eid = rs.record_evidence(store, evmod.source_evidence(repo, rel, i, commit=commit,
                                                                  meta={"check": "exclusivity (correction)"}))
            if eid:
                evs.append((eid, "supports"))
    unc = []
    if outside:
        unc.append("pattern also occurs outside the named files: "
                   + ", ".join(f"{r}:{i}" for r, i, _ in outside[:5]))
    if missing:
        unc.append("named file(s) do not match the pattern: " + ", ".join(missing))
    return {"formalized": True, "allowed_files": named, "evidence": evs,
            "status": "statically_verified" if not outside and not missing and evs else "weak_inference",
            "uncertainties": unc, "outside": [f"{r}:{i}" for r, i, _ in outside[:5]], "missing": missing}


def _audit(cl: Claims, cid: str, reason: str, actor: str, *, extra: dict | None = None) -> dict:
    """Append a history entry without changing status (claim rules still apply)."""
    c = cl.get(cid)
    fields = dict(extra or {})
    fields.setdefault("updated_at", now())
    return cl.set_status(cid, c["status"], reason=reason, actor=actor, confidence=c["confidence"],
                         downgrade=True, extra_fields=fields)


def _correct(store: Store, repo: Path, fid: str, claim: dict, *, correction: str | None, refuting_ids: list[str],
             reason: str, actor: str, snapshot: dict | None) -> dict:
    """Apply a 'corrected' verdict: supersede with the correction, or mark contradicted."""
    cl = Claims(store, repo)
    cid = claim["id"]
    commit = (snapshot or {}).get("commit_sha")
    if correction:
        info: dict = {"formalized": False}
        if claim.get("kind") == "exclusive" and (claim.get("spec") or {}).get("pattern"):
            info = _exclusive_correction(store, repo, claim, correction, commit)
        if info.get("formalized"):
            evs, status, kind, unc = info["evidence"], info["status"], "exclusive", info["uncertainties"]
        else:
            evs = [(eid, "supports") for eid in refuting_ids[:6]]
            status, kind = "weak_inference", "general"
            unc = [f"correction text supplied via feedback {fid}; the statement itself was not mechanically "
                   "verified" + (f" ({info['why']})" if info.get("why") else "")]
        new = cl.supersede(cid, correction, evidence=evs, status=status, reason=reason, actor=actor,
                           snapshot=snapshot, kind=kind)
        spec = dict(new.get("spec") or {})
        spec["from_feedback"] = fid
        subjects = list(new.get("subjects") or [])
        if info.get("formalized"):
            spec["allowed_files"] = info["allowed_files"]
            subjects += [f for f in info["allowed_files"] if f not in subjects]
        store.update_claim(new["id"], {"spec": spec, "uncertainties": list(new.get("uncertainties") or []) + unc,
                                       "subjects": subjects})
        new = cl.get(new["id"])
        return {"action": "superseded", "superseding_claim": {**_claim_brief(new), "text": new["text"],
                                                              "kind": new["kind"]},
                "correction_check": {k: v for k, v in info.items() if k != "evidence"},
                "claim_after": _claim_brief(cl.get(cid))}
    after = cl.reassess(cid, reason=reason, actor=actor)
    if after["status"] != "contradicted":
        try:
            after = cl.set_status(cid, "contradicted", reason=reason + " (refutation at least as strong as support)",
                                  actor=actor)
        except ClaimRuleError as exc:
            return {"action": "reassessed", "note": f"could not mark contradicted: {exc}",
                    "claim_after": _claim_brief(after), "corrected_statement": "unknown"}
    return {"action": "contradicted", "claim_after": _claim_brief(after), "corrected_statement": "unknown",
            "note": "no correction text given: the corrected statement stays unknown"}


def _bound_prefix(fid: str) -> str:
    return f"validity bounded (feedback {fid})"


def _retract_bounds(cl: Claims, cid: str, fid: str, verdict: str, actor: str) -> bool:
    """A re-processed feedback that no longer qualifies the claim retracts the bound it added
    earlier (the history entry records both the bound and its retraction)."""
    c = cl.get(cid)
    unc = list(c.get("uncertainties") or [])
    keep = [u for u in unc if not u.startswith(_bound_prefix(fid))]
    if len(keep) == len(unc):
        return False
    _audit(cl, cid, f"feedback {fid} re-processed as {verdict}: its earlier validity bound is retracted", actor,
           extra={"uncertainties": keep})
    return True


def _qualify(store: Store, repo: Path, cid: str, evidence_ids: list[str], bound: str, *, actor: str,
             fid: str | None = None) -> dict:
    cl = Claims(store, repo)
    for eid in evidence_ids[:8]:
        cl.attach(cid, eid, "qualifies", note=_short(bound, 200))
    c = cl.get(cid)
    unc = [u for u in (c.get("uncertainties") or []) if not (fid and u.startswith(_bound_prefix(fid)))]
    if bound not in unc:
        unc.append(bound)
    after = _audit(cl, cid, f"qualified: {_short(bound, 200)}", actor, extra={"uncertainties": unc})
    return {"action": "qualified", "qualifying_evidence": evidence_ids[:8], "uncertainty_added": bound,
            "claim_after": _claim_brief(after)}


# =============================================================================
# references (docs/DESIGN.md D16)
# =============================================================================

_DOC_CLASSES = {"paper", "doi", "doc_page", "qa_post", "web_page", "issue", "swh_object"}


def _explicit_refs(inp: dict) -> list[dict]:
    out: list[dict] = []
    if inp.get("reference"):
        out.append({"reference": inp["reference"], "ref": inp.get("ref")})
    for r in inp.get("references") or []:
        if r and r != inp.get("reference"):
            out.append({"reference": r, "ref": None})
    return out


def _resolve_refs(store: Store, repo: Path, text: str, inp: dict, *, network: str, local_intent: bool) -> dict | None:
    """Resolve the critique's references; None when the resolver is unavailable."""
    try:
        from repoatlas.references import resolve
    except ImportError:  # pragma: no cover - the package ships with RepoAtlas
        return None
    try:
        return resolve(store, repo, text or "", explicit=_explicit_refs(inp), network=network,
                       local_intent=local_intent)
    except Exception as exc:  # noqa: BLE001 - a resolver failure is reported, never fatal
        return {"id": None, "error": f"{type(exc).__name__}: {_short(exc, 200)}", "references": [],
                "questions_for_user": [], "summary": {}, "mismatches": [], "unresolved": []}


def _ref_what(r: dict) -> str:
    ident, req = r.get("identity") or {}, r.get("requested") or {}
    return (req.get("url") or ident.get("canonical_url") or ident.get("local_path")
            or (ident.get("package") or {}).get("purl") or ((ident.get("app") or {}).get("name"))
            or ((ident.get("paper") or {}).get("arxiv_id")) or req.get("path") or r.get("class") or "?")


def _ref_brief(r: dict) -> dict:
    pin = r.get("pin") or {}
    out = {"id": r["id"], "class": r["class"], "status": r["status"], "what": _short(_ref_what(r), 120),
           "pin": pin.get("display"), "basis": pin.get("basis")}
    if r.get("mismatches"):
        out["mismatches"] = [f"{m['code']} {m['severity']}: {_short(m['detail'], 160)}" for m in r["mismatches"][:4]]
    if r.get("unresolved"):
        out["unresolved"] = [{"what": u["what"], "why": _short(u["why"], 160), "next_step": u["next_step"]}
                             for u in r["unresolved"][:3]]
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def _relied(resolution: dict | None) -> list[dict]:
    return [r for r in (resolution or {}).get("references") or [] if r["class"] not in NOT_RELIED_CLASSES]


def _blocking(relied: list[dict]) -> list[dict]:
    return [r for r in relied if r["status"] in BLOCKING_STATUSES
            or any(m.get("severity") == "error" for m in r.get("mismatches") or [])]


def _is_dependency(r: dict) -> bool:
    """The pin is the version the project itself locks/installs."""
    pin = r.get("pin") or {}
    if pin.get("basis") == "local_project_version":
        return True
    return any((a.get("pin") or {}).get("basis") == "local_project_version"
               and (a.get("pin") or {}).get("value") == pin.get("value") for a in r.get("alternates") or [])


def _research_target(r: dict) -> tuple[str | None, dict | None]:
    """(reference string, pin) for ``research_full`` from a resolved reference."""
    ident, req, pin = r.get("identity") or {}, r.get("requested") or {}, r.get("pin") or {}
    p = {k: pin.get(k) for k in ("value", "kind", "name", "basis", "ref", "retrieved_at", "url") if pin.get(k)}
    if r["class"] in _DOC_CLASSES:
        url = pin.get("url") or req.get("url")
        if r["class"] == "paper" and pin.get("value"):
            url = f"https://arxiv.org/abs/{pin['value']}"
        return url, ({"url": url, **p} if url else None)
    if r["class"] == "package":
        purl = (ident.get("package") or {}).get("purl")
        if not purl or not pin.get("value"):
            return None, None
        return f"{purl}@{pin['value']}", {"version": pin["value"], "basis": pin.get("basis"),
                                          "repo": ident.get("source_repo"), "source_type": None}
    if r["class"] in ("runtime", "application", "local_file", "local_symbol"):
        return None, None
    target = ident.get("local_path") or ident.get("clone_url") or ident.get("canonical_url")
    if not target or not pin.get("value"):
        return None, None
    compare = pin.get("compare") or {}
    p["value"] = pin["value"]
    if compare:
        p["kind"], p["name"] = "compare", pin.get("name")
    p["path"] = req.get("path") if not any(m["code"] == "M1b" for m in r.get("mismatches") or []) else None
    return target, p


def _floating_disclosure(relied: list[dict]) -> list[dict]:
    out = []
    for r in relied:
        pin = r.get("pin") or {}
        if r.get("status") != "pinned_floating":
            continue
        why = {"default": "the user named no version", "explicit_floating_intent": "the user asked for the latest",
               "url_branch": "the link names a branch, not a version"}.get(pin.get("basis"), pin.get("basis") or "")
        out.append({"reference": r["id"], "pin": pin.get("display"), "basis": pin.get("basis"), "why": why})
    return out


def _with_disclosure(verdict: str, explanation: str, details: dict, ctx: dict) -> str:
    floating = _floating_disclosure(ctx.get("relied") or [])
    if not floating or verdict not in ("confirmed", "qualified"):
        return explanation
    details["floating_pins"] = floating
    parts = [f"reference {f['reference']} pinned to {f['pin']}; {f['why']}" for f in floating[:3]]
    return explanation + " Note: " + "; ".join(parts) + "."


# =============================================================================
# verdict
# =============================================================================

def decide(ctx: dict) -> tuple[str, str, list[str], dict]:
    """Deterministic verdict from the protocol results.

    ``ctx`` keys: claim (after re-check) or None, evidence (claim evidence rows
    with ``recheck_ok``), critique, reference (research result or None),
    compare (or None), props {"local", "reference"}, fid, topic, repo,
    ``relied``/``blocking`` (resolved references the critique relies on / the
    ones that are unresolved, ambiguous, refused or carry an error mismatch),
    ``stale_reason``. Returns (verdict, explanation, next_steps, details).

    Precedence: 1 no claim -> unresolved; 1b a relied-on reference is not
    pinned exactly (or conflicts) -> unresolved naming the conflict; 2 verifying
    refutation at least as strong as the best support -> corrected; 3 stale ->
    unresolved (with the actual reason); 4 reference unreachable or comparison
    failed -> unresolved; 5 claim not verified after re-check -> unresolved;
    6 proposition holds locally but its bearing is not decidable -> unresolved;
    7 topic traced on one side only -> unresolved; 8 substantive assumption
    differences (or proposition only in the reference) -> qualified; 9 document
    reference -> unresolved (needs reading); 10 confirmed. Confirmed/qualified
    verdicts resting on a floating pin disclose it.
    """
    fid, topic, claim = ctx["fid"], ctx["topic"], ctx["claim"]
    details: dict = {}
    if claim is None:
        return "unresolved", "the feedback is not linked to a claim, so there is nothing to confirm or correct", [
            "link it to a claim: repoatlas feedback add --claim <claim-id> --text ... (then process)",
            f"or ask the question directly: repoatlas analyze \"{_short(topic, 60)}\""], details
    cid = claim["id"]
    # 1b. a reference the critique relies on is not pinned exactly -> unresolved, naming the conflict
    blocking = ctx.get("blocking") or []
    if blocking:
        parts, steps = [], []
        for r in blocking[:4]:
            errs = [m for m in r.get("mismatches") or [] if m.get("severity") == "error"]
            unres = r.get("unresolved") or []
            why = "; ".join([f"{m['code']} {m['detail']}" for m in errs[:2]]
                            + [f"{u.get('reason') or 'unresolved'}: {u['why']}" for u in unres[:2]])
            parts.append(f"{r['id']} {_short(_ref_what(r), 90)} is {r['status']}" + (f" ({_short(why, 220)})" if why
                                                                                    else ""))
            steps += [u["next_step"] for u in unres[:2] if u.get("next_step")]
        qs = [q for q in (ctx.get("questions") or []) if q.get("reference") in {r["id"] for r in blocking}]
        steps += [f"ask the user: {q['question']}" + (f" ({' | '.join(q['options'][:4])})" if q.get("options") else "")
                  for q in qs[:3]]
        steps.append(f"then re-run: repoatlas feedback process {fid}")
        details["blocking_references"] = [r["id"] for r in blocking]
        return "unresolved", ("the reference(s) the critique relies on could not be inspected at an exact, "
                              "unambiguous version, so the critique cannot be checked against them: "
                              + "; ".join(parts)), list(dict.fromkeys(steps)), details
    evs = ctx["evidence"]
    sup = [e for e in evs if e["relation"] == "supports" and evmod.is_verifying(e) and e.get("recheck_ok", True)]
    ref = [e for e in evs if e["relation"] == "refutes" and evmod.is_verifying(e) and e.get("recheck_ok", True)]
    best_sup = min((_rank(e) for e in sup), default=None)
    best_ref = min((_rank(e) for e in ref), default=None)
    details.update({"best_support_rank": best_sup, "best_refutation_rank": best_ref,
                    "refuting": [f"{e['source_type']}:{e['locator']}" for e in ref[:5]]})
    crit = ctx.get("critique") or {}
    fails = [f for f in crit.get("findings", []) if f["result"] == "fail"]
    passes = [f for f in crit.get("findings", []) if f["result"] == "pass"]
    reference = ctx.get("reference")
    cmp = ctx.get("compare")
    props = ctx.get("props") or {}
    pl, pr = props.get("local") or {}, props.get("reference") or {}

    # 2. refuted by evidence at least as strong as the support -> corrected
    if ref and (best_sup is None or best_ref <= best_sup):
        top = min(ref, key=_rank)
        return "corrected", (
            f"{len(ref)} verifying refuting evidence (strongest: {top['source_type']} {top['locator']}, rank "
            f"{best_ref}) is at least as strong as the claim's best support (rank {best_sup if best_sup is not None else 'none'}); "
            "by the claim rules no verified status is allowed any more"), [
            f"review the refutation: repoatlas claim show {cid}"], details
    # 3. stale claim -> unresolved, with the reason it is stale
    if claim["status"] == "stale":
        why = ctx.get("stale_reason") or "the files behind the claim changed since its snapshot"
        return "unresolved", f"{why}; the re-check cannot judge it", [
            f"re-verify on the current tree: repoatlas verify {cid}",
            f"then re-run: repoatlas feedback process {fid}"], details
    # 4. reference unreachable, or inspected but not comparable -> unresolved
    if reference is not None and reference.get("status") != "ok":
        err = reference.get("error") or reference.get("status")
        steps = [f"fix access and retry: repoatlas feedback process {fid}"]
        steps += [s for s in reference.get("next_steps", [])[:3]]
        steps.append(f"or clone the reference yourself and compare: repoatlas compare <repo> <local-clone> --topic \"{topic}\"")
        return "unresolved", f"the reference could not be inspected ({reference.get('status')}): {_short(err, 200)}; "\
                             "the critique cannot be checked against it", steps, details
    if (reference is not None and reference.get("kind") != "document"
            and not (cmp and cmp.get("status") == "ok")):
        return "unresolved", ("the reference was inspected but the comparison with the local project failed: "
                              f"{_short(ctx.get('compare_error') or (cmp or {}).get('error') or 'unknown error', 200)}"), [
            f"check the local index: repoatlas update {ctx.get('repo') or '<repo>'}",
            f"then re-run: repoatlas feedback process {fid}"], details
    # 5. the claim must hold as verified after the re-check
    holds = claim["status"] in VERIFIED and not fails
    if not holds:
        why = "; ".join(_short(f["detail"], 100) for f in fails[:3]) or f"claim is only {claim['status']}"
        return "unresolved", f"evidence is insufficient to judge the critique: the claim does not hold as verified "\
                             f"after re-check ({why})", [
            f"strengthen or re-verify the claim: repoatlas verify {cid} (or run a targeted experiment)",
            f"then re-run: repoatlas feedback process {fid}"], details
    # 6. user's proposition holds locally but its bearing is not mechanical -> unresolved
    if pl.get("holds") and not pl.get("refutes_claim"):
        locs = ", ".join(h["at"] for h in pl.get("hits", [])[:3])
        return "unresolved", (f"the proposition /{pl['pattern']}/ holds locally ({locs}) but whether that "
                              f"contradicts a '{claim['kind']}' claim cannot be decided mechanically"), [
            f"inspect {locs}; then: repoatlas feedback resolve {fid} --verdict corrected|qualified|confirmed "
            f"--reason ... --evidence {pl['hits'][0]['evidence_id']}"], details
    # 7. a side where the topic matched no code cannot be compared (nothing vs something bounds nothing)
    if cmp and cmp.get("status") == "ok":
        untraced = [s for s in ("local", "reference") if not (cmp.get(s) or {}).get("seeds")]
        if untraced:
            suggestion = _derive_topic(claim, "")
            return "unresolved", (f"the topic '{topic}' matched no code on the {' and '.join(untraced)} side, so the "
                                  "assumptions cannot be compared"), [
                f"re-run with a topic naming the mechanism on both sides: repoatlas feedback process {fid} "
                f"--topic \"{suggestion}\"" if suggestion and suggestion != topic else
                f"re-run with a topic naming symbols that exist on both sides: repoatlas feedback process {fid} --topic ...",
                "check the reference is the right project/version for this claim"], details
    # 8. substantive differing assumptions in the reference bound the claim -> qualified
    diffs, trivial = [], 0
    if cmp and cmp.get("status") == "ok":
        for c, d in cmp["categories"].items():
            if c == "dependencies":  # a different dependency set is not a mechanism assumption; versions are
                d = {"only_local": [], "only_reference": [], "differing": d.get("differing", [])}
            else:
                sub = {k: [x for x in d[k] if not TRIVIAL_FACT.match(x["key"])] for k in ("only_local", "only_reference")}
                trivial += len(d["only_local"]) + len(d["only_reference"]) - len(sub["only_local"]) - len(sub["only_reference"])
                d = {**sub, "differing": d.get("differing", [])}
            if d["only_local"] or d["only_reference"] or d["differing"]:
                diffs.append((c, d))
    details["trivial_differences_ignored"] = trivial
    ref_only_prop = bool(pr.get("holds")) and not pl.get("holds")
    if diffs or ref_only_prop:
        label = rs.ref_label(reference) if reference else "the reference"
        parts = []
        qual_ids: list[str] = []
        for c, d in diffs:
            bits = []
            if d["only_reference"]:
                bits.append("reference only: " + ", ".join(x["key"] for x in d["only_reference"][:3]))
            if d["only_local"] and c != "dependencies":
                bits.append("local only: " + ", ".join(x["key"] for x in d["only_local"][:3]))
            if d.get("differing"):
                bits.append("differs: " + ", ".join(f"{x['key']} {x['local']} vs {x['reference']}" for x in d["differing"][:3]))
            parts.append(f"{c} ({'; '.join(bits)})")
            for x in (d["only_reference"] + d.get("differing", []) + d["only_local"])[:4]:
                qual_ids += x.get("evidence", [])[:1]
        if ref_only_prop:
            parts.append(f"proposition /{pr['pattern']}/ holds in the reference ({', '.join(h['at'] for h in pr['hits'][:2])}) but not locally")
            qual_ids += [h["evidence_id"] for h in pr["hits"][:2] if h.get("evidence_id")]
        details["qualifying_evidence"] = list(dict.fromkeys(q for q in qual_ids if q))
        details["bound"] = _short(f"{_bound_prefix(fid)}: for '{topic}' the assumptions of {label} differ - "
                                  + "; ".join(parts), 480)
        expl = (f"the claim holds locally (re-check: {len(passes)} pass, 0 fail) but the reference "
                f"{label} makes different assumptions: " + "; ".join(parts))
        return "qualified", _with_disclosure("qualified", expl, details, ctx), [
            f"read the differences: repoatlas compare <repo> {reference.get('reference') if reference else '<ref>'} --topic \"{topic}\"",
            "treat the claim as valid only under the local assumptions listed in its uncertainties"], details
    # 9. document references need a human reading -> unresolved
    if reference is not None and reference.get("kind") == "document":
        pids = [p["evidence_id"] for p in reference.get("passages", [])[:3]]
        return "unresolved", ("the claim holds locally, but a document's statements cannot be compared with code "
                              "mechanically" + (f"; relevant passages recorded: {', '.join(pids)}" if pids else
                                                "; no passage mentions the topic")), [
            "read the recorded passages (repoatlas feedback show " + fid + ")",
            f"then: repoatlas feedback resolve {fid} --verdict qualified|confirmed --reason ... --evidence <id>"], details
    # 10. confirmed
    why = [f"re-check passed ({len(passes)} pass, 0 fail)", "no refuting evidence at least as strong as the support"]
    if pl.get("holds") is False:
        why.append(f"proposition /{pl['pattern']}/ does not hold locally (0 hits in {pl['files_scanned']} files"
                   + (f" matching {pl['in']}" if pl.get("in") else "") + ")")
    if pr.get("holds") is False:
        why.append("nor in the reference checkout")
    if cmp and cmp.get("status") == "ok":
        why.append("no differing assumptions in the traced mechanism of the reference"
                   + (f" ({trivial} trivial difference(s) in literals/parameter types ignored)" if trivial else ""))
    expl = "; ".join(why) + ". The critique is recorded (user_feedback, qualifies) but not supported by evidence."
    return "confirmed", _with_disclosure("confirmed", expl, details, ctx), [], details


# =============================================================================
# public API
# =============================================================================

def add(store: Store, repo: Path, text: str, *, claim_id: str | None = None, reference: str | None = None,
        ref: str | None = None, correction: str | None = None, expect_pattern: str | None = None,
        expect_in: str | None = None, references: list[str] | None = None) -> dict:
    """Record a user critique (open). The claim is not changed.

    ``reference``/``ref`` name one reference (``ref`` applies to it);
    ``references`` adds more (``URL[@ref]`` strings). They and the references
    in ``text`` are resolved offline right away; conflicts show up in
    ``warnings`` and ``resolution.references``.
    """
    repo = Path(repo).resolve()
    if not text or not text.strip():
        raise ValueError("feedback text is empty")
    cl = Claims(store, repo)
    if claim_id:
        cl.get(claim_id)  # KeyError when unknown
    fid = new_id("fbk")
    ts = now()
    refs = [r for r in (references or []) if r]
    inp = {"reference": reference, "ref": ref, "references": refs or None, "correction": correction,
           "expect_pattern": expect_pattern, "expect_in": expect_in}
    warnings = []
    if expect_pattern:
        try:
            re.compile(expect_pattern)
        except re.error as exc:
            warnings.append(f"expect_pattern is not a valid regex ({exc}); it will be reported, not evaluated")
    eid = evmod.add(store, {
        "source_type": "user_feedback", "locator": f"feedback {fid}", "content_hash": evmod.content_hash(text),
        "excerpt": text[:evmod.EXCERPT_MAX], "commit_sha": None,
        "meta": {"feedback_id": fid, **{k: v for k, v in inp.items() if v}},
    })
    resolution: dict = {"input": inp, "user_evidence_id": eid, "warnings": warnings, "runs": [],
                        "log": [{"at": ts, "action": "added", "actor": "user"}]}
    rr = _resolve_refs(store, repo, text, inp, network="off", local_intent=bool(claim_id))
    if rr is not None and (rr.get("references") or rr.get("error")):
        resolution["references"] = {
            "resolution_id": rr.get("id"), "status": rr.get("status"), "summary": rr.get("summary"),
            "references": [_ref_brief(r) for r in rr.get("references") or []][:8],
            "mismatches": [f"{m['reference']} {m['code']} {m['severity']}: {_short(m['detail'], 160)}"
                           for m in rr.get("mismatches") or []][:8],
            "questions_for_user": rr.get("questions_for_user") or [], "error": rr.get("error")}
        for m in (rr.get("mismatches") or [])[:5]:
            if m.get("severity") in ("warn", "error"):
                warnings.append(f"reference {m['reference']}: {m['code']} {_short(m['detail'], 160)}")
        for r in _blocking(_relied(rr)):
            warnings.append(f"reference {r['id']} ({_short(_ref_what(r), 80)}) is {r['status']} offline; "
                            "`feedback process` resolves it again with the network allowed")
    store.insert("feedback", {
        "id": fid, "claim_id": claim_id, "text": text, "reference": reference or (refs[0] if refs else None),
        "status": "open", "verdict": None, "resolution": resolution, "created_at": ts, "updated_at": ts,
    })
    if claim_id:
        cl.attach(claim_id, eid, "qualifies", note=f"user feedback {fid}: a hypothesis, not evidence")
    return show(store, fid)


def _stale_reason(claim: dict, evs: list[dict], store: Store) -> str | None:
    """Why a claim is stale, precisely: cited lines that no longer match, or its last stale transition."""
    changed = [e for e in evs if e["relation"] == "supports" and e.get("recheck_ok") is False]
    if changed:
        locs = ", ".join(f"{e['locator']} ({e.get('recheck_status') or 'changed'})" for e in changed[:3])
        return f"the lines the claim cites no longer match: {locs}"
    for h in reversed(store.history(claim["id"])):
        if h["to_status"] == "stale":
            return f"the claim was marked stale: {_short(h['reason'], 160)}"
    return None


def _proposition_hits_cited_lines(repo: Path, claim: dict, evs: list[dict], pl: dict) -> list[dict]:
    """Proposition hits inside lines a supporting citation covers whose content changed since it was cited."""
    out = []
    changed = [e for e in evs if e["relation"] == "supports" and e.get("recheck_status") in ("changed", "gone")
               and e.get("path") and e.get("line_start")]
    for h in pl.get("hits") or []:
        for e in changed:
            a, b = int(e["line_start"]), int(e.get("line_end") or e["line_start"])
            if h["path"] == e["path"] and a <= h["line_no"] <= b:
                out.append({"hit": h, "cited": e["locator"]})
                break
    return out


def process(store: Store, repo: Path, feedback_id: str, *, topic: str | None = None,
            network: str | None = None) -> dict:
    """Run the verification protocol for one feedback item (see module docstring).

    ``network`` (``off`` | ``cache`` | ``on``, default: config ``research.network`` or ``cache``) governs
    reference resolution; a named version that cannot be pinned makes the verdict ``unresolved``.
    """
    from repoatlas import critique, index, workflow

    repo = Path(repo).resolve()
    row = _row(store, feedback_id)
    fid = row["id"]
    res0 = row["resolution"]
    inp = res0.get("input") or {}
    actor = f"feedback:{fid}"
    cl = Claims(store, repo)
    run: dict = {"started_at": now(), "steps": []}
    steps = run["steps"]
    produced: list[str] = []  # evidence created by this run (the only foreign evidence `resolve` may use)
    warnings: list[str] = []
    if network is None:
        try:
            from repoatlas.paths import load_config

            network = ((load_config(repo).get("research") or {}).get("network")) or "cache"
        except Exception:  # noqa: BLE001
            network = "cache"
    if network not in ("off", "cache", "on"):
        network = "cache"

    def step(n: int, name: str, result) -> None:
        steps.append({"n": n, "name": name, "result": result})

    # 1. prior claim --------------------------------------------------------------
    claim = None
    cid = row.get("claim_id")
    if cid:
        try:
            shown = cl.show(cid)
            claim = cl.get(cid)
            step(1, "prior_claim", {
                "id": cid, "text": shown["text"], "status": shown["status"], "confidence": shown["confidence"],
                "kind": shown["kind"], "subjects": shown["subjects"][:6],
                "spec": {k: v for k, v in (shown["spec"] or {}).items() if k in ("pattern", "allowed_files", "at", "target_label", "ceiling")},
                "supporting": shown["supporting"][:5], "refuting": shown["refuting"][:5],
                "superseded_by": shown["superseded_by"], "history_entries": len(shown["history"])})
        except KeyError as exc:
            step(1, "prior_claim", {"error": str(exc)})
    else:
        step(1, "prior_claim", {"claim": None, "note": "feedback is not linked to a claim"})
    before = _claim_brief(claim)
    topic_used = topic or _derive_topic(claim, row["text"])
    run["topic"] = topic_used

    # refresh the local index + snapshot so re-checks see the current tree
    g, snap = None, store.latest_snapshot()
    try:
        up = workflow.update(store, repo)
        snap = up.get("snapshot") or snap
        run["local_index"] = {"mode": up.get("mode"), "snapshot": (snap or {}).get("id"),
                              "commit": (snap or {}).get("commit_sha"),
                              "stale_marked": [s["id"] if isinstance(s, dict) else s
                                               for s in (up.get("stale") or [])][:10]}
        if up.get("error"):
            run["local_index"].update(error=up["error"], hint=up.get("hint"))
            warnings.append(f"the local index was not refreshed: {_short(up['error'], 200)}"
                            + (f"; run: {up['hint']}" if up.get("hint") else ""))
        g = index.load(repo)
    except Exception as exc:
        run["local_index"] = {"error": f"{type(exc).__name__}: {_short(exc, 200)}"}
        warnings.append(f"the local index could not be refreshed: {run['local_index']['error']}")
    commit = (snap or {}).get("commit_sha")

    # 2-3. references resolved to exact pins, then inspected at those pins ---------------------------------------
    ref_full = None
    resolution = _resolve_refs(store, repo, row["text"], inp, network=network, local_intent=claim is not None)
    relied = _relied(resolution)
    blocking = _blocking(relied)
    researched: list[dict] = []
    skipped: list[str] = []
    if resolution is None and inp.get("reference"):  # resolver unavailable: the previous single-reference path
        ref_full = rs.research_full(store, repo, inp["reference"], ref=inp.get("ref"), topic=topic_used)
    elif not blocking:
        n_code = n_doc = 0
        code_first = sorted(relied, key=lambda r: r["class"] in _DOC_CLASSES)
        for r in code_first:
            if r["status"] not in ("pinned", "pinned_floating"):
                continue
            target, pin = _research_target(r)
            if not target:
                continue
            is_doc = r["class"] in _DOC_CLASSES
            if (is_doc and n_doc >= MAX_DOC_REFERENCES) or (not is_doc and n_code >= MAX_CODE_REFERENCES):
                skipped.append(f"{r['id']} {_short(_ref_what(r), 80)}: research budget reached")
                continue
            n_doc, n_code = n_doc + is_doc, n_code + (not is_doc)
            pin = {**pin, "resolution_id": resolution.get("id"), "reference_id": r["id"]}
            if _is_dependency(r) and not is_doc:
                pin["source_type"] = "dependency_source"
            full = rs.research_full(store, repo, target, topic=topic_used, pin=pin, network=network)
            produced += [e for e in full.get("evidence_ids") or [] if e]
            researched.append({"reference": r["id"], "pin": (r.get("pin") or {}).get("display"),
                               "basis": (r.get("pin") or {}).get("basis"), "research_id": full["id"],
                               "status": full["status"], "source_type": full.get("source_type"),
                               "error": full.get("error")})
            if ref_full is None or (ref_full.get("kind") == "document" and full.get("kind") != "document"):
                ref_full = full
    if resolution is not None and (relied or resolution.get("error")):
        c = rs.compact(ref_full) if ref_full else {}
        step(2, "inspect_reference", {
            **{k: c.get(k) for k in ("id", "status", "kind", "source_type", "pin", "requested_ref",
                                     "resolved_commit", "resolved_tag", "checkout", "error", "warnings",
                                     "next_steps", "evidence_count", "version", "content_hash", "title", "passages",
                                     "compare_pins", "changed_files") if c.get(k) not in (None, [], {})},
            "resolution_id": resolution.get("id"), "resolution_status": resolution.get("status"),
            "references": [_ref_brief(r) for r in relied][:8], "researched": researched,
            **({"skipped": skipped} if skipped else {}),
            **({"blocking": [r["id"] for r in blocking]} if blocking else {}),
            **({"resolver_error": resolution["error"]} if resolution.get("error") else {})})
    elif ref_full is not None:
        c = rs.compact(ref_full)
        produced += [e for e in ref_full.get("evidence_ids") or [] if e]
        step(2, "inspect_reference", {k: c.get(k) for k in (
            "id", "status", "kind", "source_type", "pin", "requested_ref", "resolved_commit", "resolved_tag",
            "checkout", "error", "warnings", "next_steps", "evidence_count", "version", "content_hash", "title",
            "passages") if c.get(k) not in (None, [], {})})
    else:
        step(2, "inspect_reference", {"skipped": "no reference given"})

    # 4-6. mechanism, assumptions, why ---------------------------------------------------------
    local_mech = None
    if g is not None:
        try:
            local_mech = rs.mechanism(g, topic_used, repo, commit)
        except Exception as exc:
            run["local_mechanism_error"] = f"{type(exc).__name__}: {_short(exc, 200)}"
    ref_mech = (ref_full or {}).get("_mechanism")
    lm, rm = rs.compact_mechanism(local_mech, 5), rs.compact_mechanism(ref_mech, 5)
    step(4, "trace_mechanism", {"topic": topic_used,
                                "local": lm and {k: lm[k] for k in ("seeds", "symbols", "edges", "coverage")},
                                "reference": rm and {k: rm[k] for k in ("seeds", "symbols", "edges")}})
    step(5, "assumptions", {"local": lm and {"facts": lm["facts"], "counts": lm["fact_counts"]},
                            "reference": rm and {"facts": rm["facts"], "counts": rm["fact_counts"]}})
    step(6, "why", {"local": lm and lm["why"], "reference": rm and rm["why"]})

    # 7. compare -----------------------------------------------------------------------------
    cmp, compare_error = None, None
    if ref_full and ref_full["status"] == "ok" and ref_full.get("kind") != "document":
        try:
            cmp = rs.compare_with(store, repo, ref_full, topic_used)
            produced += [e for e in ref_full.get("evidence_ids") or [] if e]
            for d in cmp["categories"].values():
                for key in ("only_local", "only_reference", "differing"):
                    for x in d.get(key) or []:
                        produced += [e for e in x.get("evidence") or [] if e]
            step(7, "compare", {
                "status": cmp["status"], "claims": cmp.get("claims"), "unknowns": cmp.get("unknowns", [])[:6],
                "categories": {c: {"counts": d["counts"],
                                   "only_local": [(x["key"], x["at"][:1]) for x in d["only_local"][:5]],
                                   "only_reference": [(x["key"], x["at"][:1]) for x in d["only_reference"][:5]],
                                   "differing": [(x["key"], x["local"], x["reference"]) for x in d.get("differing", [])[:5]]}
                               for c, d in cmp["categories"].items()}})
        except Exception as exc:
            compare_error = f"{type(exc).__name__}: {_short(exc, 200)}"
            step(7, "compare", {"error": compare_error})
    else:
        step(7, "compare", {"skipped": "reference not pinned exactly (see step 2)" if blocking else
                            "no code reference to compare with" if not ref_full else
                            f"reference {ref_full['status']}" if ref_full["status"] != "ok" else "document reference"})

    # 8. re-challenge + propositions --------------------------------------------------------------
    crit = None
    if claim is not None:
        try:
            crit = critique.challenge(store, repo, cid, graph=g, actor=actor)
            ordered = sorted(crit["findings"], key=lambda f: {"fail": 0, "warn": 1}.get(f["result"], 2))
            step(8, "rechallenge", {"before": crit["before"], "after": crit["after"],
                                    "findings": [{k: f[k] for k in ("check", "result", "detail")} for f in ordered[:12]],
                                    "findings_total": len(ordered),
                                    "refuting_evidence_added": crit["refuting_evidence_added"]})
        except Exception as exc:
            step(8, "rechallenge", {"error": f"{type(exc).__name__}: {_short(exc, 200)}"})
    from repoatlas.evidence import check_source

    def claim_evidence(c_id: str) -> list[dict]:
        out = []
        for e in cl.evidence(c_id):
            e = dict(e)
            if e.get("path") and e.get("line_start") and e["source_type"] in (
                    "source_code", "reference_repo", "dependency_source", "design_doc"):
                chk = check_source(repo, e)
                e["recheck_ok"], e["recheck_status"] = chk.ok, chk.status
            out.append(e)
        return out

    props: dict = {}
    if inp.get("expect_pattern"):
        props["local"] = _eval_proposition(store, repo, inp["expect_pattern"], inp.get("expect_in"), commit=commit,
                                           external=False)
        produced += [h["evidence_id"] for h in props["local"].get("hits") or [] if h.get("evidence_id")]
        if ref_full and ref_full.get("status") == "ok" and ref_full.get("_root"):
            props["reference"] = _eval_proposition(store, Path(ref_full["_root"]), inp["expect_pattern"],
                                                   inp.get("expect_in"), commit=ref_full.get("resolved_commit"),
                                                   external=True)
            produced += [h["evidence_id"] for h in props["reference"].get("hits") or [] if h.get("evidence_id")]
        pl = props["local"]
        if claim is not None and pl.get("holds"):
            spec = claim.get("spec") or {}
            if claim["kind"] == "exclusive" and spec.get("pattern"):
                crx = re.compile(spec["pattern"])
                allowed = set(spec.get("allowed_files") or [])
                refuting = [h for h in pl["hits"] if h["path"] not in allowed
                            and PurePosixPath(h["path"]).suffix in _EXCLUSIVE_SUFFIXES
                            and crx.search(evmod.read_lines(repo / h["path"], h["line_no"], h["line_no"]) or "")]
                for h in refuting:
                    if h.get("evidence_id"):
                        cl.attach(cid, h["evidence_id"], "refutes",
                                  note=f"feedback {fid}: proposition hit outside the allowed files")
                pl["refutes_claim"] = bool(refuting)
            if not pl.get("refutes_claim"):
                # the user's proposition holds exactly where the claim's citation used to say something else
                at_cited = _proposition_hits_cited_lines(repo, claim, claim_evidence(cid), pl)
                for x in at_cited:
                    if x["hit"].get("evidence_id"):
                        cl.attach(cid, x["hit"]["evidence_id"], "refutes",
                                  note=f"feedback {fid}: the cited lines {x['cited']} changed and the user's "
                                       "proposition holds there now")
                if at_cited:
                    pl["refutes_claim"] = True
                    pl["refutes_via"] = "cited lines changed; the proposition holds at them now: " + ", ".join(
                        f"{x['hit']['at']} (was {x['cited']})" for x in at_cited[:3])
            if not pl.get("refutes_claim"):
                for h in pl["hits"][:3]:
                    if h.get("evidence_id"):
                        cl.attach(cid, h["evidence_id"], "qualifies",
                                  note=f"feedback {fid}: user proposition holds here (bearing not decided mechanically)")
        step(8, "propositions", props)
    else:
        step(8, "propositions", {"skipped": "no checkable proposition (expect_pattern) given"})

    # decide ----------------------------------------------------------------------------------------
    claim_now = cl.get(cid) if claim is not None else None
    evs = claim_evidence(cid) if claim_now is not None else []
    ctx = {"fid": fid, "topic": topic_used, "claim": claim_now, "evidence": evs, "critique": crit,
           "reference": ref_full, "compare": cmp, "compare_error": compare_error, "props": props, "repo": repo,
           "relied": relied, "blocking": blocking, "questions": (resolution or {}).get("questions_for_user") or [],
           "stale_reason": _stale_reason(claim_now, evs, store) if claim_now and claim_now["status"] == "stale"
           else None}
    verdict, explanation, next_steps, details = decide(ctx)
    if (props.get("local") or {}).get("error"):
        next_steps = next_steps + [f"the proposition was not evaluated ({props['local']['error']}); "
                                   f"re-add the feedback with a valid --expect-pattern"]
    if (run.get("local_index") or {}).get("hint"):
        next_steps = next_steps + [f"refresh the local index: {run['local_index']['hint']}"]
    if (props.get("local") or {}).get("refutes_via"):
        details["refutes_via"] = props["local"]["refutes_via"]
    step(8, "decide", {"verdict": verdict, "explanation": explanation, "details": details})

    # 9. apply through Claims (append-only history) --------------------------------------------------
    applied: dict = {"action": "none"}
    try:
        if claim_now is not None:
            reason = f"feedback {fid}: {verdict} - {_short(explanation, 220)}"
            if verdict == "corrected":
                ref_ids = [e["id"] for e in evs if e["relation"] == "refutes" and evmod.is_verifying(e)
                           and e.get("recheck_ok", True)]
                applied = _correct(store, repo, fid, claim_now, correction=inp.get("correction"),
                                   refuting_ids=ref_ids, reason=reason, actor=actor, snapshot=snap)
            elif verdict == "qualified":
                applied = _qualify(store, repo, cid, details.get("qualifying_evidence", []), details["bound"],
                                   actor=actor, fid=fid)
            else:
                retracted = _retract_bounds(cl, cid, fid, verdict, actor)
                after = _audit(cl, cid, reason, actor)
                applied = {"action": "recorded", "claim_after": _claim_brief(after),
                           **({"retracted_earlier_bound": True} if retracted else {})}
    except Exception as exc:  # the verdict stands; report the failed update
        applied = {"action": "error", "error": f"{type(exc).__name__}: {_short(exc, 200)}"}
        next_steps = next_steps + ["claim update failed; apply it manually with `repoatlas feedback resolve`"]
    step(9, "apply", applied)

    run.update({"finished_at": now(), "verdict": verdict, "explanation": explanation, "next_steps": next_steps,
                "claim_before": before, "claim_after": _claim_brief(cl.get(cid)) if claim_now else None,
                "superseding_claim": applied.get("superseding_claim"),
                "evidence_ids": list(dict.fromkeys(produced)), "warnings": warnings,
                "resolution_id": (resolution or {}).get("id"), "network": network})
    resolution_row = dict(res0)
    resolution_row["runs"] = list(res0.get("runs") or []) + [run]
    resolution_row["latest"] = {k: run[k] for k in ("verdict", "explanation", "next_steps", "topic", "finished_at")}
    resolution_row["log"] = list(res0.get("log") or []) + [{"at": run["finished_at"], "action": "processed",
                                                            "verdict": verdict, "actor": "feedback.process"}]
    store.update("feedback", fid, {"status": "processed", "verdict": verdict, "resolution": resolution_row,
                                   "updated_at": run["finished_at"]})
    return {"id": fid, "status": "processed", "verdict": verdict, "explanation": explanation,
            "next_steps": next_steps, "topic": topic_used,
            "claim": {"id": cid, "before": before, "after": run["claim_after"]} if cid else None,
            "superseding_claim": applied.get("superseding_claim"), "applied": applied,
            "steps": steps, **({"warnings": warnings} if warnings else {}),
            **({"references": [_ref_brief(r) for r in relied][:8]} if relied else {})}


def _own_evidence(res0: dict) -> set[str]:
    """Evidence ids this feedback's protocol runs produced."""
    out: set[str] = set()
    for run in res0.get("runs") or []:
        out.update(e for e in run.get("evidence_ids") or [] if e)
    return out


def resolve(store: Store, repo: Path, feedback_id: str, verdict: str, *, reason: str, evidence_ids: list[str],
            correction: str | None = None) -> dict:
    """Set a verdict manually. Rejected (nothing changes) unless the evidence rules are met.

    ``confirmed``/``qualified`` accept only evidence already linked to the claim or produced by this
    feedback's own protocol runs, and never raise the claim's status.
    """
    repo = Path(repo).resolve()
    row = _row(store, feedback_id)
    fid = row["id"]
    res0 = row["resolution"]

    def reject(why: str) -> dict:
        return {"id": fid, "status": "rejected", "verdict": verdict, "error": why,
                "feedback_status": row["status"], "current_verdict": row.get("verdict")}

    if verdict not in VERDICTS:
        return reject(f"verdict must be one of {VERDICTS}")
    if not (reason or "").strip():
        return reject("a reason is required")
    evs = []
    for eid in dict.fromkeys(evidence_ids or []):
        e = store.evidence(eid)
        if e is None:
            return reject(f"evidence {eid} does not exist")
        evs.append(e)
    usable = [e for e in evs if e["source_type"] not in UNUSABLE_FOR_VERDICT]
    cid = row.get("claim_id")
    if verdict in NEEDS_EVIDENCE:
        if not evs:
            return reject(f"verdict '{verdict}' requires at least one existing evidence id (--evidence)")
        if not usable:
            return reject("user feedback, search results and model summaries cannot justify a verdict")
        if verdict in ("confirmed", "corrected") and not any(evmod.is_verifying(e) for e in usable):
            return reject(f"'{verdict}' needs verifying evidence; given only: "
                          + ", ".join(sorted({e['source_type'] for e in usable})))
        if not cid:
            return reject("the feedback is not linked to a claim")
        if verdict in ("confirmed", "qualified"):
            linked = {e["id"]: e.get("relation") for e in store.claim_evidence(cid)}
            own = _own_evidence(res0)
            foreign = [e["id"] for e in usable if e["id"] not in linked and e["id"] not in own]
            if foreign:
                return reject(f"evidence {', '.join(foreign)} is neither linked to claim {cid} nor produced by this "
                              f"feedback's protocol run; foreign evidence cannot {verdict[:-1]} a claim it was never "
                              f"checked against (run `repoatlas feedback process {fid}` or cite the claim's evidence)")
            if verdict == "confirmed":
                against = [e["id"] for e in usable if linked.get(e["id"]) == "refutes"]
                if against:
                    return reject(f"evidence {', '.join(against)} refutes claim {cid}; it cannot confirm it")
    actor = f"feedback-resolve:{fid}"
    cl = Claims(store, repo)
    applied: dict = {"action": "none"}
    correction = correction or (res0.get("input") or {}).get("correction")
    full_reason = f"feedback {fid} resolved {verdict}: {_short(reason, 220)}"
    if cid:
        claim = cl.get(cid)
        if verdict == "corrected":
            for e in usable:
                cl.attach(cid, e["id"], "refutes", note=f"feedback {fid} (manual resolution)")
            applied = _correct(store, repo, fid, cl.get(cid), correction=correction,
                               refuting_ids=[e["id"] for e in usable], reason=full_reason, actor=actor,
                               snapshot=store.latest_snapshot())
        elif verdict == "qualified":
            applied = _qualify(store, repo, cid, [e["id"] for e in usable],
                               _short(f"{_bound_prefix(fid)}: {reason}", 480), actor=actor, fid=fid)
        elif verdict == "confirmed":
            _retract_bounds(cl, cid, fid, verdict, actor)
            # the critique is rejected; the claim keeps (never exceeds) the status it had
            if claim["status"] in ORDER:
                after = cl.reassess(cid, reason=full_reason, actor=actor, ceiling=claim["status"])
            else:
                after = cl.get(cid)
            if after["status"] == claim["status"] and abs(after["confidence"] - claim["confidence"]) < 1e-9:
                after = _audit(cl, cid, full_reason, actor)
            applied = {"action": "confirmed", "claim_after": _claim_brief(after),
                       "evidence_cited": [e["id"] for e in usable]}
        else:
            retracted = _retract_bounds(cl, cid, fid, verdict, actor)
            after = _audit(cl, cid, full_reason, actor)
            applied = {"action": "recorded", "claim_after": _claim_brief(after),
                       **({"retracted_earlier_bound": True} if retracted else {})}
    ts = now()
    resolution = dict(res0)
    resolution["manual"] = {"verdict": verdict, "reason": reason, "evidence_ids": [e["id"] for e in evs],
                            "correction": correction, "at": ts, "applied": applied}
    resolution["log"] = list(res0.get("log") or []) + [{"at": ts, "action": "resolved", "verdict": verdict,
                                                        "reason": _short(reason, 200), "actor": "resolve",
                                                        "evidence_ids": [e["id"] for e in evs]}]
    store.update("feedback", fid, {"status": "resolved", "verdict": verdict, "resolution": resolution,
                                   "updated_at": ts})
    return {"id": fid, "status": "resolved", "verdict": verdict, "reason": reason,
            "evidence_ids": [e["id"] for e in evs], "claim": _claim_brief(cl.get(cid)) if cid else None,
            "superseding_claim": applied.get("superseding_claim"), "applied": applied}


def show(store: Store, feedback_id: str) -> dict:
    row = _row(store, feedback_id)
    res = row["resolution"]
    runs = res.get("runs") or []
    latest = runs[-1] if runs else None
    claim = store.claim(row["claim_id"]) if row.get("claim_id") else None
    return {
        "id": row["id"], "status": row["status"], "verdict": row.get("verdict"), "text": row["text"],
        "claim_id": row.get("claim_id"), "claim": _claim_brief(claim), "reference": row.get("reference"),
        "input": res.get("input"), "user_evidence_id": res.get("user_evidence_id"),
        "warnings": res.get("warnings") or [],
        "latest": latest and {
            "verdict": latest.get("verdict"), "explanation": latest.get("explanation"),
            "next_steps": latest.get("next_steps"), "topic": latest.get("topic"),
            "finished_at": latest.get("finished_at"), "claim_before": latest.get("claim_before"),
            "claim_after": latest.get("claim_after"), "superseding_claim": latest.get("superseding_claim"),
            "steps": [{"n": s["n"], "name": s["name"], "summary": _step_summary(s)} for s in latest.get("steps", [])],
        },
        "runs": len(runs), "manual": res.get("manual"), "log": res.get("log") or [],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_feedback(store: Store, limit: int = 100) -> list[dict]:
    rows = store.all("SELECT id, status, verdict, claim_id, text, created_at, updated_at FROM feedback"
                     " ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    return [{**{k: r[k] for k in ("id", "status", "verdict", "claim_id", "created_at", "updated_at")},
             "text": _short(r["text"], 100)} for r in rows]


# =============================================================================
# rendering
# =============================================================================

def _step_summary(s: dict) -> str:
    r = s.get("result")
    if not isinstance(r, dict):
        return _short(r, 120)
    if r.get("skipped"):
        return "skipped: " + str(r["skipped"])
    if r.get("error"):
        return "error: " + _short(r["error"], 120)
    name = s["name"]
    if name == "prior_claim":
        return f"{r.get('id')} [{r.get('status')}] {_short(r.get('text'), 80)}" if r.get("id") else _short(r.get("note"), 100)
    if name == "inspect_reference":
        return f"{r.get('status')} {r.get('kind') or ''} {_short(r.get('pin') or r.get('error') or '', 110)}"
    if name == "trace_mechanism":
        loc = (r.get("local") or {}).get("seeds") or []
        ref = (r.get("reference") or {}).get("seeds") or []
        return f"topic '{r.get('topic')}': local seeds {', '.join(loc[:3]) or '-'}; reference seeds {', '.join(ref[:3]) or '-'}"
    if name == "assumptions":
        lc = (r.get("local") or {}).get("counts") or {}
        rc = (r.get("reference") or {}).get("counts") or {}
        return "local " + ", ".join(f"{k}={v}" for k, v in lc.items()) + (
            "; reference " + ", ".join(f"{k}={v}" for k, v in rc.items()) if rc else "")
    if name == "why":
        lw = r.get("local") or {}
        return f"local: {len(lw.get('commits', []))} commits, {len(lw.get('tests', []))} tests, {len(lw.get('docs', []))} docs"
    if name == "compare":
        cats = r.get("categories") or {}
        return ", ".join(f"{c}: -{d['counts']['only_local']}/+{d['counts']['only_reference']}"
                         + (f"/~{d['counts']['differing']}" if d["counts"].get("differing") else "")
                         for c, d in cats.items()) or str(r.get("status"))
    if name == "rechallenge":
        agg: dict[str, dict[str, int]] = {}
        for f in r.get("findings", []):
            agg.setdefault(f["check"], {}).setdefault(f["result"], 0)
            agg[f["check"]][f["result"]] += 1
        return (f"{r['before']['status']} -> {r['after']['status']}; "
                + ", ".join(f"{c}=" + "/".join(f"{n} {res}" if n > 1 else res for res, n in v.items())
                            for c, v in agg.items()))
    if name == "propositions":
        out = []
        for side in ("local", "reference"):
            p = r.get(side)
            if p:
                out.append(f"{side}: " + (p.get("error") or f"holds={p['holds']} ({p['hit_count']} hits)"))
        return "; ".join(out)
    if name == "decide":
        return f"{r.get('verdict')}: {_short(r.get('explanation'), 120)}"
    if name == "apply":
        return f"{r.get('action')}" + (f" -> {r['superseding_claim']['id']}" if r.get("superseding_claim") else "")
    return _short(r, 120)


def render(res) -> None:
    if isinstance(res, list):
        if not res:
            print("no feedback recorded")
        for r in res:
            print(f"{r['id']} [{r['status']}{'/' + r['verdict'] if r.get('verdict') else ''}] "
                  f"claim {r.get('claim_id') or '-'}: {r['text']}")
        return
    if res.get("status") == "rejected":
        print(f"REJECTED {res['id']} ({res.get('verdict')}): {res['error']}")
        print(f"  feedback stays {res.get('feedback_status')}" + (f"/{res['current_verdict']}" if res.get("current_verdict") else ""))
        return
    print(f"feedback {res['id']}: {res['status']}" + (f" -> {res['verdict']}" if res.get("verdict") else ""))
    if res.get("text"):
        print(f"  critique: {_short(res['text'], 200)}")
    if res.get("explanation"):
        print(f"  why: {res['explanation']}")
    if res.get("reason"):
        print(f"  reason: {res['reason']}")
    c = res.get("claim")
    if c and isinstance(c, dict) and "before" in c:
        b, a = c.get("before") or {}, c.get("after") or {}
        print(f"  claim {c['id']}: {b.get('status')} {b.get('confidence')} -> {a.get('status')} {a.get('confidence')}")
    elif c:
        print(f"  claim {c['id']}: {c['status']} {c['confidence']}"
              + (f" (superseded by {c['superseded_by']})" if c.get("superseded_by") else ""))
    sc = res.get("superseding_claim")
    if sc:
        print(f"  superseded by {sc['id']} [{sc['status']}]: {_short(sc.get('text'), 140)}")
    latest = res.get("latest")
    if latest:
        print(f"  latest run: {latest['verdict']} - {_short(latest['explanation'], 200)}")
        for s in latest.get("steps", []):
            print(f"    {s['n']} {s['name']}: {s['summary']}")
        for n in latest.get("next_steps") or []:
            print(f"  next: {n}")
    for s in res.get("steps", []):
        print(f"    {s['n']} {s['name']}: {_step_summary(s)}")
    for n in res.get("next_steps") or []:
        print(f"  next: {n}")
    for w in res.get("warnings") or []:
        print(f"  warning: {w}")
