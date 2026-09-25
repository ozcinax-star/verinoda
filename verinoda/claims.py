"""Claims: every important conclusion, its status, confidence and evidence.

Status rules (enforced by :func:`check_status`, tested in tests/test_claims.py):

* Every ``*_verified`` status needs one *evidence group* that is verifying,
  fresh (cited lines still match, relocated through their anchor when they
  only moved) and fully graded by :mod:`verinoda.entail` - the evidence must
  mechanically state the claim, not merely exist. Documentary kinds
  (``decision``, ``history``) can never be entailed mechanically; for them
  ``primary_source_verified`` needs their best grade, attribution (the record
  states what the claim attributes to it).
* ``experiment_verified``   such a group with an experiment/test run that passed.
* ``statically_verified``   such a group of source lines (or a static resolver's
  definitive answer) that re-check OK now.
* ``primary_source_verified`` such a group with a primary source (docs, standard,
  design doc, git history, dependency/reference source at a pinned version).
* ``observed``              any such group.
* ``strong_inference``      at least one relevant (graded ``partial`` or better)
  supporting evidence that is not a search result, model summary or user
  feedback (a graph edge between the claimed endpoints is enough here - that is
  what inference means; a search hit or a summary is not). Text a user or an
  agent wrote (``spec.free_text``, ``claim add``) that no typed check covers
  needs evidence graded ``full`` (a verbatim quote): its word overlap with the
  cited lines allows ``weak_inference`` only.
* ``weak_inference``        at least one supporting evidence of any kind.
* ``unknown``               nothing: a claim without evidence is unknown.
* ``contradicted``          at least one *definitive* refutation (an exhaustive
  check within a stated scope). A *heuristic* refutation (``meta.strength =
  "heuristic"``) never contradicts: it lowers the claim one step
  (verified -> strong_inference -> weak_inference -> unknown).
* ``stale``                 is set only by invalidation (``create`` refuses it).

Evidence groups (FEVER evidence sets): ``claim_evidence.grp`` names a group;
without one each evidence is its own group, except a flow claim's hop and sink
lines, which form one group - a flow is verified only when every hop is.

Every claim records two levels in ``spec``: ``assessed`` - the status its
creator asked for (or ``strong_inference`` for a request outside the ordered
list), fixed at creation and raised only by an explicit, rule-checked
``set_status(..., downgrade=False)`` - and ``ceiling`` - the highest status
re-verification may restore it to, which critique only ever lowers. Critique
also stores its confidence ``penalty``; re-verification applies it, so a
verify never raises confidence back above what critique left. Confidences are
stored rounded to two decimals. A rejected explicit status request is recorded
in the claim's history before the error is raised.

Invalidation (docs/DESIGN.md D24) works on *claim dependencies* recorded at
creation (:func:`derive_deps`): symbol facets (signature, body), import
bindings, module statements and document sections from
:mod:`verinoda.anchors`, falling back to whole files where no facts exist.
An update marks a claim stale only when a facet it depends on changed or its
cited evidence no longer relocates to identical text; a claim whose files
changed but whose facets did not is re-bound to the new snapshot (backdated)
without a status change, and the history says so.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from verinoda import anchors, entail
from verinoda import evidence as evmod
from verinoda.store import Store, new_id, now

STATUSES = (
    "observed", "experiment_verified", "statically_verified", "primary_source_verified",
    "strong_inference", "weak_inference", "unknown", "contradicted", "stale",
)
VERIFIED = {"observed", "experiment_verified", "statically_verified", "primary_source_verified"}
ORDER = [  # strongest first; used to find the best status the evidence allows
    "experiment_verified", "statically_verified", "primary_source_verified", "observed",
    "strong_inference", "weak_inference", "unknown",
]
CONFIDENCE_CAP = {
    "experiment_verified": 0.95, "statically_verified": 0.9, "observed": 0.9,
    "primary_source_verified": 0.85, "strong_inference": 0.7, "weak_inference": 0.4,
    "unknown": 0.0, "contradicted": 0.05, "stale": 0.3,
}
PRIMARY_TYPES = {"official_doc", "standard", "design_doc", "git_history", "dependency_source",
                 "reference_repo", "paper"}
SOURCE_LINE_TYPES = ("source_code", "dependency_source", "reference_repo")
STATIC_TYPES = SOURCE_LINE_TYPES + ("static_resolution",)
# Evidence whose cited lines are re-read before any status decision.
RECHECK_TYPES = evmod.FILE_TYPES + ("static_resolution",)
# Not even inference: a search hit, a model's summary or a user's say-so is a
# pointer to evidence, never support by itself.
NOT_INFERENCE_SUPPORT = evmod.NOT_SUPPORT
# A requested status outside ORDER (contradicted, a typo) never earns more than this.
DEFAULT_CEILING = "strong_inference"
CODE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php", ".cs", ".kt",
                 ".scala", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".lua")


class ClaimRuleError(ValueError):
    pass


def step_down(status: str) -> str:
    """One step weaker: any verified status -> strong_inference -> weak_inference -> unknown."""
    if status in VERIFIED:
        return "strong_inference"
    return {"strong_inference": "weak_inference", "weak_inference": "unknown"}.get(status, status)


def _supports(evs: list[dict]) -> list[dict]:
    return [e for e in evs if e.get("relation") == "supports"]


def _refutes(evs: list[dict]) -> list[dict]:
    return [e for e in evs if e.get("relation") == "refutes"]


def strength(ev: dict) -> str:
    """``definitive`` (the default for refutations recorded before strengths existed) or ``heuristic``."""
    return "heuristic" if (ev.get("meta") or {}).get("strength") == "heuristic" else "definitive"


def _live(evs: list[dict], source_checks: dict[str, bool]) -> list[dict]:
    """Evidence minus file/line records whose re-check against the tree failed.

    A cited block that no longer matches (changed, deleted, or moved without an
    anchor to prove it is the same code) is not support - nor refutation - for
    the *current* code any more.
    """
    return [e for e in evs if source_checks.get(e.get("id"), True)]


def groups(evs: list[dict], kind: str | None = None) -> dict[str, list[dict]]:
    """Supporting evidence by group (see the module docstring)."""
    out: dict[str, list[dict]] = {}
    for e in _supports(evs):
        g = e.get("grp")
        meta = e.get("meta") or {}
        if not g and kind == "flow" and (meta.get("hop") or meta.get("sink")):
            g = "flow"
        out.setdefault(g or f"ev:{e.get('id') or e.get('locator')}", []).append(e)
    return out


class _Ctx:
    def __init__(self, evs: list[dict], checks: dict[str, bool] | None, grades: dict[str, str] | None,
                 kind: str | None, claim: dict | None = None):
        self.evs = evs
        self.checks = checks or {}
        self.grades = grades
        self.kind = kind or "general"
        spec = (claim or {}).get("spec") or {}
        # text a user or an agent wrote (`claim add`) that no typed check covers: word overlap with the
        # cited lines is not support for it, so it needs a verbatim quote for strong_inference
        self.written = bool(spec.get("free_text")) and self.kind not in entail.DOCUMENTARY and \
            not entail.typed(self.kind, spec, (claim or {}).get("text"))
        live = _live(evs, self.checks)
        self.sup = _supports(live)
        self.ref_def = [e for e in _refutes(live) if strength(e) == "definitive"]
        self.ref_heur = [e for e in _refutes(live) if strength(e) == "heuristic"]
        self.groups = groups(evs, self.kind)

    def grade(self, e: dict) -> str:
        if self.grades is None:
            return "none"
        return self.grades.get(e.get("id") or e.get("locator"), "none")

    def live(self, e: dict) -> bool:
        return self.checks.get(e.get("id"), True)


def _group_problem(ctx: _Ctx, members: list[dict], status: str) -> str | None:
    """Why this group cannot carry ``status`` (None when it can)."""
    need = "partial" if (status == "primary_source_verified" and ctx.kind in entail.DOCUMENTARY) else "full"
    if any(not evmod.is_verifying(e) for e in members):
        return "not all of it is verifying evidence (" + ", ".join(sorted({evmod.effective_type(e) for e in members
                                                                             if not evmod.is_verifying(e)})) + ")"
    stale = [e for e in members if not ctx.live(e)]
    if stale:
        return f"{len(stale)} cited source block(s) no longer match"
    weak = [] if ctx.grades is None else [e for e in members if not entail.at_least(ctx.grade(e), need)]
    if weak:
        return (f"it does not {'state' if need == 'full' else 'attribute'} the claim "
                f"(graded {', '.join(sorted({ctx.grade(e) for e in weak}))})")
    types = {evmod.effective_type(e) for e in members}
    if status == "experiment_verified":
        if not any(evmod.effective_type(e) in ("experiment", "test_result")
                   and (e.get("meta") or {}).get("outcome") == "pass" for e in members):
            return "no experiment/test run in it passed"
    elif status == "statically_verified":
        if not types <= set(STATIC_TYPES):
            return "it is not source lines of code"
        if not any((evmod.effective_type(e) in SOURCE_LINE_TYPES and e.get("path") and e.get("content_hash"))
                   or evmod.effective_type(e) == "static_resolution" for e in members):
            return "no cited source lines in it"
    elif status == "primary_source_verified":
        if not types & PRIMARY_TYPES:
            return "no primary source in it"
    worst = max(evmod.rank(e) for e in members)
    if any(evmod.rank(r) <= worst and evmod.is_verifying(r) for r in ctx.ref_def):
        return "refuting evidence at least as strong as this support exists"
    return None


def _allows(status: str, ctx: _Ctx) -> str | None:
    """The evidence rules, ignoring heuristic refutations (see :func:`check_status`)."""
    if status not in STATUSES:
        return f"unknown status {status!r}"
    if status in VERIFIED:
        verifying = [e for e in ctx.sup if evmod.is_verifying(e)]
        if not verifying:
            kinds = sorted({evmod.effective_type(e) for e in ctx.sup}) or ["none"]
            stale = [e for e in _supports(ctx.evs) if not ctx.live(e)]
            return (f"{status} requires verifying evidence; supporting evidence is only: {', '.join(kinds)}"
                    + (f" ({len(stale)} cited source block(s) no longer match)" if stale else ""))
        problems = []
        for gid, members in ctx.groups.items():
            why = _group_problem(ctx, members, status)
            if why is None:
                return None
            problems.append(why)
        blocked = [p for p in problems if p.startswith("refuting evidence")]
        if blocked:
            return "refuting evidence at least as strong as the best support exists"
        base = {"experiment_verified": "experiment_verified requires a supporting experiment/test run that passed",
                "statically_verified": "statically_verified requires supporting source lines that still match "
                                       "their hash",
                "primary_source_verified": "primary_source_verified requires a supporting primary source",
                "observed": "observed requires verifying evidence"}[status]
        return f"{base} and states the claim; best group: {sorted(problems, key=len)[0]}"
    if status == "strong_inference":
        usable = [e for e in ctx.sup if evmod.effective_type(e) not in NOT_INFERENCE_SUPPORT]
        if not usable:
            if ctx.sup:
                return ("strong_inference requires supporting evidence other than "
                        + ", ".join(sorted({evmod.effective_type(e) for e in ctx.sup})))
            return "strong_inference requires at least one supporting evidence"
        if ctx.grades is not None and ctx.written and not any(entail.at_least(ctx.grade(e), "full") for e in usable):
            return ("strong_inference for written text needs a verbatim quote ('contains: ...') or a typed claim "
                    "kind; word overlap with the cited lines allows weak_inference")
        if ctx.grades is not None and not any(entail.at_least(ctx.grade(e), "partial") for e in usable):
            return "strong_inference requires relevant evidence; the supporting evidence is not about the claim"
    elif status == "weak_inference":
        if not _supports(ctx.evs):
            return "weak_inference requires at least one supporting evidence (a claim without evidence is unknown)"
    elif status == "contradicted":
        if not ctx.ref_def:
            if ctx.ref_heur:
                return "contradicted requires a definitive refutation; the refutation is only heuristic"
            return "contradicted requires at least one refuting evidence"
    return None


def _top(ctx: _Ctx) -> str:
    for st in ORDER:
        if _allows(st, ctx) is None:
            return st
    return "unknown"


def check_status(status: str, evs: list[dict], source_checks: dict[str, bool] | None = None, *,
                 claim: dict | None = None, repo: Path | None = None, grades: dict[str, str] | None = None
                 ) -> str | None:
    """Return None when ``status`` is allowed for this evidence, else the reason.

    ``claim`` (with ``repo``) grades the evidence for relevance (or pass
    precomputed ``grades``). Without either, only the evidence types, groups,
    liveness and refutations are checked - a pure query about evidence. Every
    status that reaches the store goes through :meth:`Claims.set_status`,
    which always passes the claim, so irrelevant evidence cannot verify a
    stored claim on any path.
    """
    if grades is None and claim is not None:
        grades = grade_evidence(claim, evs, repo)
    ctx = _Ctx(evs, source_checks, grades, (claim or {}).get("kind"), claim)
    why = _allows(status, ctx)
    if why is not None:
        return why
    if ctx.ref_heur and status in ORDER:
        lowered = step_down(_top(ctx))
        if ORDER.index(status) < ORDER.index(lowered):
            return f"a heuristic refutation lowers the claim one step (to {lowered})"
    return None


def best_status(evs: list[dict], ceiling: str | None = None,
                source_checks: dict[str, bool] | None = None, *, claim: dict | None = None,
                repo: Path | None = None, grades: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Strongest status the evidence allows, not above ``ceiling``.

    Definitive refutation with no live support at all gives ``contradicted``.
    """
    if grades is None and claim is not None:
        grades = grade_evidence(claim, evs, repo)
    ctx = _Ctx(evs, source_checks, grades, (claim or {}).get("kind"), claim)
    if ctx.ref_def and not ctx.sup:
        return "contradicted", ["only refuting evidence is recorded"]
    reasons: list[str] = []
    start = ORDER.index(ceiling) if ceiling in ORDER else 0
    for st in ORDER[start:]:
        why = check_status(st, evs, source_checks, claim=claim, repo=repo, grades=grades)
        if why is None:
            return st, reasons
        reasons.append(why)
    return "unknown", reasons


def grade_evidence(claim: dict, evs: list[dict], repo: Path | None) -> dict[str, str]:
    """Entailment grade of every supporting evidence row for this claim."""
    out: dict[str, str] = {}
    for e in _supports(evs):
        out[e.get("id") or e.get("locator")] = entail.grade(
            claim.get("kind") or "general", repo, claim.get("spec") or {}, e,
            text=claim.get("text"), subjects=claim.get("subjects") or [])
    return out


def claim_key(kind: str, spec: dict | None, subjects: list[str] | None, text: str) -> str:
    """Line-independent identity of what a claim states (kind + semantic spec, or normalised text)."""
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in sorted(v.items())
                    if k not in ("at", "ceiling", "assessed", "penalty", "from_feedback", "experiment")}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    sem = strip(spec or {})
    body = sem if sem else re.sub(r"\s+", " ", entail._LOCATOR.sub("", text or "")).strip().lower()
    raw = json.dumps([kind, body, sorted(subjects or [])], sort_keys=True, default=str)
    return "ck:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Claims:
    def __init__(self, store: Store, repo: Path | None = None):
        self.store = store
        self.repo = Path(repo).resolve() if repo else None

    # -- creation ----------------------------------------------------------
    def create(
        self, text: str, *, project: str, snapshot: dict | None, subjects: list[str] | None = None,
        status: str = "unknown", confidence: float | None = None,
        evidence: list[tuple] | None = None, uncertainties: list[str] | None = None,
        kind: str = "general", spec: dict | None = None, analysis_id: str | None = None,
        valid_version: str | None = None,
        valid_env: str | None = None, supersedes: str | None = None, actor: str = "verinoda",
    ) -> dict:
        """Create a claim; ``evidence`` items are ``(evidence, relation)`` or ``(evidence, relation, group)``."""
        if status == "stale":
            raise ClaimRuleError("stale is set only by invalidation (update/scan/verify), not requested at creation")
        # The assessed level: what the creator judged, fixed for the claim's
        # life. A request outside ORDER (contradicted without refutation, a
        # typo) is capped at strong_inference so it can never rise further.
        assessed = status if status in ORDER else DEFAULT_CEILING
        ts = now()
        cid = new_id("clm")
        spec_in = {k: v for k, v in (spec or {}).items() if k not in ("ceiling", "assessed", "penalty")}
        self.store.insert_claim({
            "id": cid, "text": text, "project": project,
            "snapshot_id": snapshot["id"] if snapshot else None,
            "commit_sha": snapshot["commit_sha"] if snapshot else None,
            "subjects": subjects or [], "status": "unknown", "confidence": 0.0,
            "valid_version": valid_version or (snapshot or {}).get("commit_sha"),
            "valid_env": valid_env, "uncertainties": uncertainties or [], "kind": kind,
            # ceiling: later re-verification may restore up to this status but
            # never escalate past what the original analysis judged (critique
            # may lower it). assessed: the original judgement, never lowered.
            "spec": {**spec_in, "ceiling": assessed, "assessed": assessed},
            "analysis_id": analysis_id, "supersedes": supersedes,
            "claim_key": claim_key(kind, spec_in, subjects, text),
            "created_at": ts, "updated_at": ts,
        })
        for item in evidence or []:
            ev, rel = item[0], item[1]
            grp = item[2] if len(item) > 2 else None
            self._link(cid, ev, rel, None, grp)
        self._record_deps(cid)
        self.set_status(cid, status, reason="initial assessment", actor=actor,
                        confidence=confidence, downgrade=True)
        return self.get(cid)

    def _link(self, cid: str, ev: dict | str, relation: str, note: str | None, grp: str | None) -> str:
        if relation not in evmod.RELATIONS:
            raise ClaimRuleError(f"relation must be one of {evmod.RELATIONS}")
        eid = ev if isinstance(ev, str) else evmod.add(self.store, ev)
        self.store.link(cid, eid, relation, note, grp)
        return eid

    def attach(self, cid: str, ev: dict | str, relation: str, note: str | None = None,
               grp: str | None = None) -> str:
        """Link evidence (new or an existing id) to a claim; ``grp`` names its evidence group.

        Attaching never changes the status by itself: the next status change
        (reassess, verify, critique) applies the rules, which grade the new
        evidence for relevance like any other.
        """
        eid = self._link(cid, ev, relation, note, grp)
        self._record_deps(cid)
        return eid

    def _record_deps(self, cid: str) -> None:
        if self.repo is None:
            return
        c = self.get(cid)
        try:
            deps = derive_deps(self.store, self.repo, c, self.evidence(cid))
        except (OSError, ValueError, RecursionError):
            return  # no deps -> file-level rules, never less safe
        if deps:
            self.store.add_claim_deps(cid, deps)

    # -- reads -------------------------------------------------------------
    def get(self, cid: str) -> dict:
        c = self.store.claim(cid)
        if c is None:
            raise KeyError(f"no claim {cid}")
        return c

    def evidence(self, cid: str) -> list[dict]:
        return self.store.claim_evidence(cid)

    def grades(self, cid: str) -> dict[str, str]:
        c = self.get(cid)
        return grade_evidence(c, self.evidence(cid), self.repo)

    def show(self, cid: str) -> dict:
        c = self.get(cid)
        evs = self.evidence(cid)
        grades = grade_evidence(c, evs, self.repo) if self.repo is not None else {}

        def summ(e: dict) -> dict:
            loc = self.store.latest_evidence_location(e["id"]) if e.get("path") else None
            return evmod.summarize(e, location=loc, grade=grades.get(e["id"]) if e["relation"] == "supports"
                                   else None)

        out = {
            **{k: c[k] for k in ("id", "text", "status", "kind", "spec", "project",
                                 "snapshot_id", "commit_sha", "subjects", "valid_version",
                                 "valid_env", "uncertainties", "supersedes", "superseded_by",
                                 "created_at", "updated_at")},
            "confidence": round(c["confidence"] or 0.0, 2),
            "supporting": [summ(e) for e in evs if e["relation"] == "supports"],
            "refuting": [summ(e) for e in evs if e["relation"] == "refutes"],
            "qualifying": [summ(e) for e in evs if e["relation"] == "qualifies"],
            "history": [
                {k: h[k] for k in ("from_status", "to_status", "to_confidence", "reason", "actor", "created_at")}
                for h in self.store.history(cid)
            ],
        }
        if c.get("verified_at"):
            out["verified_at"] = c["verified_at"]
        return out

    # -- status changes ----------------------------------------------------
    def _source_checks(self, evs: list[dict], *, record: bool = True) -> dict[str, bool]:
        """Re-check cited lines now; anchored relocations are appended to ``evidence_locations``."""
        if self.repo is None:
            return {}
        out: dict[str, bool] = {}
        for e in evs:
            if e["source_type"] in RECHECK_TYPES and e.get("path") and e.get("line_start"):
                chk = evmod.check_source(self.repo, e)
                out[e["id"]] = chk.ok
                if record and chk.status != "same":
                    try:
                        evmod.record_location(self.store, e, chk)
                    except Exception:  # bookkeeping must never block a status decision
                        pass
        return out

    def set_status(
        self, cid: str, status: str, *, reason: str, actor: str = "verinoda",
        confidence: float | None = None, downgrade: bool = False, payload: dict | None = None,
        extra_fields: dict | None = None,
    ) -> dict:
        """Apply ``status`` if the evidence allows it.

        With ``downgrade=True`` a disallowed status falls back to the best one
        the evidence permits (recording why); otherwise the refusal is recorded
        in the claim's history and :class:`ClaimRuleError` is raised.

        An explicit (``downgrade=False``) status that the evidence allows and
        that is stronger than the recorded assessment is a new assessment: the
        claim's ``assessed`` level and ``ceiling`` are raised to it, so a later
        re-verification or critique starts from what was actually established.

        Without an explicit ``confidence`` the status cap minus the stored
        critique penalty is used, so re-verification never undoes critique.
        """
        c = self.get(cid)
        requested = status
        why = None
        if status != "stale":  # invalidation needs no evidence check
            evs = self.evidence(cid)
            checks = self._source_checks(evs)
            grades = grade_evidence(c, evs, self.repo)
            why = check_status(status, evs, checks, claim=c, repo=self.repo, grades=grades)
        notes = []
        if why is None and not downgrade and status in ORDER and self._raise_assessment(c, status):
            c = self.get(cid)
        if why is not None:
            if not downgrade:
                self.store.record_transition(
                    cid, from_status=c["status"], to_status=c["status"], from_conf=c["confidence"],
                    to_conf=c["confidence"], reason=f"requested {status} rejected: {why}", actor=actor,
                    payload={**(payload or {}), "rejected": status, "why": why, "request_reason": reason})
                raise ClaimRuleError(why)
            if status in ORDER:
                status, extra = best_status(evs, status, checks, claim=c, repo=self.repo, grades=grades)
            elif c["status"] in ORDER:
                # e.g. "contradicted" without refuting evidence: a refused
                # downgrade must never *raise* the claim above where it is.
                status, extra = best_status(evs, c["status"], checks, claim=c, repo=self.repo, grades=grades)
            else:  # stale / contradicted stay as they are
                status, extra = c["status"], []
            notes = [why, *extra]
        cap = CONFIDENCE_CAP[status]
        penalty = float((c.get("spec") or {}).get("penalty") or 0.0)
        if confidence is None:
            # Invalidation (stale/contradicted) never *raises* confidence.
            conf = min(cap, c["confidence"] or 0.0) if status in ("stale", "contradicted") else max(0.0, cap - penalty)
        else:
            conf = max(0.0, min(confidence, cap))
        conf = round(conf, 2)
        fields = dict(extra_fields or {})
        if status in VERIFIED:
            fields.setdefault("verified_at", fields.get("snapshot_id") or c.get("snapshot_id") or now())
        unchanged = c["status"] == status and abs((c["confidence"] or 0) - conf) < 1e-9
        if unchanged and not extra_fields and requested == status:  # a refused request is always recorded
            if fields.get("verified_at") and fields["verified_at"] != c.get("verified_at"):
                self.store.update_claim(cid, {"verified_at": fields["verified_at"]})
            return self.get(cid)
        full_reason = reason if not notes else f"{reason} (downgraded: {notes[0]})"
        if len(notes) > 1 and notes[-1] != notes[0]:  # and why it stopped where it did
            full_reason = f"{full_reason[:-1]}; {notes[-1]})"
        self.store.record_transition(
            cid, from_status=c["status"], to_status=status, from_conf=c["confidence"],
            to_conf=conf, reason=full_reason, actor=actor,
            payload={**(payload or {}), **({"rule_notes": notes, "requested": requested} if notes else {})},
            extra_fields=fields,
        )
        if status in ("stale", "contradicted"):
            from verinoda.memory import Memory
            Memory(self.store).invalidate_for_claim(cid, f"source claim became {status}")
        return self.get(cid)

    def _raise_assessment(self, c: dict, status: str) -> bool:
        spec = dict(c.get("spec") or {})
        changed = False
        for key in ("assessed", "ceiling"):
            cur = spec.get(key)
            if cur in ORDER and ORDER.index(status) < ORDER.index(cur):
                spec[key] = status
                changed = True
        if changed:
            self.store.update_claim(c["id"], {"spec": spec})
        return changed

    def assessed(self, c: dict) -> str:
        """The claim's original assessment (see the module docstring).

        Claims recorded before ``assessed`` existed fall back to the status the
        initial assessment produced (from history), then to the ceiling.
        """
        spec = c.get("spec") or {}
        if spec.get("assessed") in ORDER:
            return spec["assessed"]
        for h in self.store.history(c["id"]):
            if h["reason"].startswith("initial assessment") and h["to_status"] in ORDER:
                return h["to_status"]
        if spec.get("ceiling") in ORDER:
            return spec["ceiling"]
        return c["status"] if c["status"] in ORDER else DEFAULT_CEILING

    def lower_ceiling(self, cid: str, status: str) -> None:
        c = self.get(cid)
        spec = dict(c.get("spec") or {})
        cur = spec.get("ceiling")
        if status in ORDER and (cur not in ORDER or ORDER.index(status) > ORDER.index(cur)):
            spec["ceiling"] = status
            self.store.update_claim(cid, {"spec": spec})

    def set_penalty(self, cid: str, penalty: float) -> None:
        """Persist critique's confidence penalty (applied by every later status change)."""
        c = self.get(cid)
        spec = dict(c.get("spec") or {})
        penalty = round(max(0.0, float(penalty)), 2)
        if abs(float(spec.get("penalty") or 0.0) - penalty) > 1e-9:
            spec["penalty"] = penalty
            self.store.update_claim(cid, {"spec": spec})

    def reassess(self, cid: str, *, reason: str, actor: str = "verinoda", ceiling: str | None = None) -> dict:
        c = self.get(cid)
        stored = (c.get("spec") or {}).get("ceiling")
        if stored in ORDER and (ceiling not in ORDER or ORDER.index(stored) > ORDER.index(ceiling)):
            ceiling = stored
        evs = self.evidence(cid)
        checks = self._source_checks(evs)
        if c.get("superseded_by"):
            # A superseded claim stays contradicted: the correction replaced it.
            return self.set_status(cid, "contradicted", reason=reason + f" (superseded by {c['superseded_by']})",
                                   actor=actor, downgrade=True)
        live = _live(evs, checks)
        sup = _supports(live)
        ref = [e for e in _refutes(live) if strength(e) == "definitive"]
        if ref and not sup:
            return self.set_status(cid, "contradicted", reason=reason, actor=actor)
        if ref and sup:
            best_sup = min(evmod.rank(e) for e in sup)
            best_ref = min(evmod.rank(e) for e in ref)
            if best_ref < best_sup:  # stronger source refutes than supports
                return self.set_status(cid, "contradicted", reason=reason + " (refutation outranks support)",
                                       actor=actor)
        grades = grade_evidence(c, evs, self.repo)
        st, _ = best_status(evs, ceiling, checks, claim=c, repo=self.repo, grades=grades)
        if st == "contradicted":
            return self.set_status(cid, "contradicted", reason=reason, actor=actor)
        return self.set_status(cid, st, reason=reason, actor=actor)

    def supersede(self, old_id: str, new_text: str, *, evidence: list[tuple], status: str, reason: str,
                  actor: str, snapshot: dict | None, kind: str | None = None) -> dict:
        """Create a corrected claim; the old one stays, marked contradicted."""
        old = self.get(old_id)
        new = self.create(
            new_text, project=old["project"], snapshot=snapshot, subjects=old["subjects"],
            status=status, evidence=evidence, kind=kind or old["kind"], spec=old.get("spec"),
            supersedes=old_id, actor=actor,
        )
        # What supports the correction refutes the original.
        for e in self.store.claim_evidence(new["id"]):
            if e["relation"] == "supports":
                self.store.link(old_id, e["id"], "refutes", note=f"correction {new['id']}")
        self.set_status(old_id, "contradicted", reason=reason, actor=actor, downgrade=True,
                        extra_fields={"superseded_by": new["id"]}, payload={"superseded_by": new["id"]})
        return new


# -- claim dependencies (D24) --------------------------------------------------------

def _is_test_file(path: str) -> bool:
    from verinoda.architecture_map import is_test_file

    return is_test_file(path)


def testset_fp(files: dict[str, str]) -> str:
    """Fingerprint of the test files (path and content) in a file map."""
    h = hashlib.sha256()
    for p in sorted(p for p in files if _is_test_file(p)):
        h.update(f"{p}\0{files[p]}\n".encode("utf-8"))
    return h.hexdigest()[:24]


def _subject_parts(s: str) -> tuple[str, str | None]:
    path, sep, sym = str(s).partition("::")
    return path, (sym.strip() if sep else None)


def _looks_like_path(s: str) -> bool:
    return "/" in s or bool(Path(s).suffix)


def derive_deps(store: Store, repo: Path, claim: dict, evs: list[dict] | None = None) -> list[dict]:
    """What a claim's truth depends on, per claim kind (docs/DESIGN.md D24).

    Returns ``[{"dep_key", "facet", "fp", "scheme"}]`` computed against the
    claim's snapshot (a file whose current content is not that snapshot's
    content, and whose facts are not cached, becomes a ``file:`` dependency).
    """
    repo = Path(repo)
    evs = evs if evs is not None else store.claim_evidence(claim["id"])
    kind = claim.get("kind") or "general"
    spec = claim.get("spec") or {}
    subjects = [str(s) for s in claim.get("subjects") or []]
    snap_files = store.snapshot_files(claim["snapshot_id"]) if claim.get("snapshot_id") else None
    deps: dict[tuple[str, str], dict] = {}
    facts_cache: dict[str, dict | None] = {}

    def sha_of(path: str) -> str | None:
        if snap_files is not None:
            return snap_files.get(path)
        try:
            return hashlib.sha256((repo / path).read_bytes()).hexdigest()
        except OSError:
            return None

    def facts(path: str) -> dict | None:
        if path not in facts_cache:
            sha = sha_of(path)
            facts_cache[path] = anchors.facts_for(store, repo, path, sha256=sha) if sha else None
        return facts_cache[path]

    def add(key: str, facet: str, fp: str | None, scheme: str) -> None:
        deps[(key, facet)] = {"dep_key": key, "facet": facet, "fp": fp, "scheme": scheme}

    def add_file(path: str) -> None:
        add(f"file:{path}", "content", sha_of(path) or anchors.ABSENT, "file")

    def add_facets(path: str, key: str, facets: tuple[str, ...]) -> None:
        f = facts(path)
        if not anchors.usable(f):
            add_file(path)
            return
        for facet in facets:
            add(key, facet, anchors.facet_fp(f, key, facet), f["scheme"])

    def add_enclosing(path: str, a: int, b: int, facet: str = "body") -> None:
        f = facts(path)
        where = anchors.enclosing(f, a, b)
        if where is None:
            add_file(path)
        elif where[0] == "sym":
            add_facets(path, anchors.sym_key(path, where[1]), (facet,))
        elif where[0] == "mod":
            add_facets(path, f"mod:{path}::{where[1]}", ("stmt",))
        else:
            for k in anchors.sections_overlapping(f, a, b) or [where[1]]:
                add_facets(path, f"sec:{path}::{k}", ("text",))

    def add_binding(path: str, name: str) -> None:
        if name and not path.endswith(anchors.MD_SUFFIXES):
            add_facets(path, f"bind:{path}::{name}", ("bind",))

    def add_called_bindings(path: str, a: int, b: int, fallback: str) -> None:
        """Bindings of the names actually called at ``a..b`` (an alias is the name that matters)."""
        text = None
        sha = sha_of(path)
        try:
            data = (repo / path).read_bytes()
            if sha is None or hashlib.sha256(data).hexdigest() == sha:
                text = data.decode("utf-8", errors="replace")
        except OSError:
            pass
        if text is None:
            add_file(path)  # the snapshot's version is not on disk: file level
            return
        names: list[str] = []
        for ln in range(a, b + 1):
            got = entail.names_called_at(text, path, ln)
            if got is None:
                names = []
                break
            names += got
        for name in dict.fromkeys(names or [fallback]):
            add_binding(path, name)

    def add_target(subject: str | None, label: str | None) -> None:
        if not subject:
            return
        path, sym = _subject_parts(subject)
        token = entail._token(label or sym or "")
        if not path or not _looks_like_path(path):
            return
        f = facts(path)
        if not anchors.usable(f) or not token:
            add_file(path)
            return
        found = anchors.symbols_named(f, token)
        if not found:
            add_file(path)
            return
        for q, _ in found:
            add_facets(path, anchors.sym_key(path, q), ("sig",))
        add_binding(path, token)

    local = [e for e in evs if e.get("path") and e.get("line_start") and not (e.get("meta") or {}).get("root")
             and e["source_type"] in RECHECK_TYPES]
    local_sup = [e for e in local if e["relation"] in ("supports", "refutes")]

    def ev_range(e: dict) -> tuple[int, int]:
        return int(e["line_start"]), int(e.get("line_end") or e["line_start"])

    if kind == "location":
        for e in local_sup:
            add_enclosing(e["path"], *ev_range(e), facet="sig")
    elif kind == "relation":
        at = spec.get("at")
        token = entail._token(spec.get("target_label") or "")
        sites = []
        if at and ":" in at and at.rpartition(":")[2].isdigit():
            p, _, ln = at.rpartition(":")
            sites.append((p, int(ln), int(ln), (spec.get("alias") or token)))
        for e in local_sup:
            a, b = ev_range(e)
            sites.append((e["path"], a, b, (e.get("meta") or {}).get("alias") or token))
        for p, a, b, name in sites:
            if (repo / p).is_file() or (snap_files and p in snap_files):
                add_enclosing(p, a, b, "body")
                add_called_bindings(p, a, b, name)
                if token and name != token:
                    add_binding(p, token)
        add_target(subjects[1] if len(subjects) > 1 else None, spec.get("target_label"))
    elif kind == "flow":
        for e in local_sup:
            a, b = ev_range(e)
            add_enclosing(e["path"], a, b, facet="body")
            hop = (e.get("meta") or {}).get("hop")
            if hop:
                add_called_bindings(e["path"], a, b, entail._token(str(hop).partition("->")[2]))
        for h in spec.get("hops") or []:
            p, _, ln = str(h.get("at") or "").rpartition(":")
            if p and ln.isdigit():
                add_enclosing(p, int(ln), int(ln), "body")
                add_called_bindings(p, int(ln), int(ln), entail._token(str(h.get("to") or "")))
    elif kind == "config":
        for e in local_sup:
            add_enclosing(e["path"], *ev_range(e), facet="body")
    elif kind == "exclusive":
        add("scope:code", "any", None, "scope")
        for e in local_sup:
            add_enclosing(e["path"], *ev_range(e), facet="body")
    elif kind in ("tests", "test_run"):
        test_files = sorted({str(t).split("::")[0] for t in (spec.get("tests") or spec.get("command") or [])
                             if ".py" in str(t)} | {e["path"] for e in local if _is_test_file(e["path"])} |
                            {_subject_parts(s)[0] for s in subjects if _is_test_file(_subject_parts(s)[0])})
        if spec.get("watch") == "tests":
            add("testset", "tests", testset_fp(snap_files) if snap_files is not None else None, "testset")
        closure = entail._import_closure(repo, test_files) if test_files else set()
        paths = set(closure) | {_subject_parts(s)[0] for s in subjects if _looks_like_path(_subject_parts(s)[0])}
        paths |= {e["path"] for e in local}
        if kind == "tests" and spec.get("tests") and any(not p.endswith(".py") for p in paths):
            add("scope:code", "any", None, "scope")  # no import closure outside Python: any code change
        for p in sorted(paths):
            add_file(p)
    elif kind == "decision":
        for e in local_sup:
            a, b = ev_range(e)
            f = facts(e["path"])
            secs = anchors.sections_overlapping(f, a, b)
            if secs:
                for k in secs:
                    add_facets(e["path"], f"sec:{e['path']}::{k}", ("text",))
            else:
                add_file(e["path"])
    elif kind == "history":
        for e in evs:
            if evmod.effective_type(e) == "git_history" and e.get("commit_sha"):
                add(f"commit:{e['commit_sha']}", "exists", e["commit_sha"], "git")
        for e in local_sup:
            add_file(e["path"])
    elif kind == "impact":
        for s in subjects:
            p, _ = _subject_parts(s)
            if _looks_like_path(p):
                add_file(p)
        for e in evs:
            if e.get("path") and not (e.get("meta") or {}).get("root"):
                add_file(e["path"])
    else:  # general and any other kind
        for e in local_sup:
            add_enclosing(e["path"], *ev_range(e), facet="body")
        for s in subjects:
            p, sym = _subject_parts(s)
            if not _looks_like_path(p):
                continue
            if sym:
                f = facts(p)
                found = anchors.symbols_named(f, entail._token(sym)) if anchors.usable(f) else []
                if found:
                    for q, _ in found:
                        add_facets(p, anchors.sym_key(p, q), ("body",))
                    continue
            add_file(p)
    if spec.get("watch") == "tests" and ("testset", "tests") not in deps:
        add("testset", "tests", testset_fp(snap_files) if snap_files is not None else None, "testset")
    return list(deps.values())


def _new_facts(store: Store, repo: Path | None, path: str, sha: str | None) -> dict | None:
    if not sha:
        return None
    hit = anchors.facts_by_sha(store, path, sha)
    if hit is not None or repo is None:
        return hit
    return anchors.facts_for(store, repo, path, sha256=sha)


def dep_changes(store: Store, repo: Path | None, deps: list[dict], old_files: dict[str, str],
                new_files: dict[str, str], *, partial: bool = False) -> list[dict]:
    """Facets among ``deps`` that differ in ``new_files`` (``partial``: skip global deps the map cannot judge).

    A dependency whose new facts are unavailable, or were computed with
    another scheme, counts as changed when its file changed (file-level
    fallback: never less safe than before).
    """
    out: list[dict] = []
    for d in deps:
        kind, path, _ = anchors.split_key(d["dep_key"])
        if kind == "commit":
            continue
        if kind == "scope":
            if partial:
                continue
            ch = sorted(p for p in set(old_files) | set(new_files)
                        if old_files.get(p) != new_files.get(p) and p.endswith(CODE_SUFFIXES))
            if ch:
                out.append({"dep": d["dep_key"], "facet": d["facet"], "change": "modified",
                            "files": ch[:10], "file": ch[0]})
            continue
        if kind == "testset":
            if partial and not any(_is_test_file(p) for p in set(new_files) | set(old_files)):
                continue
            if d.get("fp") is None or testset_fp(new_files) != d["fp"]:
                for p in sorted(p for p in set(old_files) | set(new_files)
                                if _is_test_file(p) and old_files.get(p) != new_files.get(p)):
                    change = "added" if p not in old_files else "removed" if p not in new_files else "modified"
                    out.append({"dep": "testset", "facet": "tests", "change": change, "file": p})
            continue
        if path not in old_files and path not in new_files:
            continue  # untracked on both sides: the evidence re-check decides
        old_sha, new_sha = old_files.get(path), new_files.get(path)
        if old_sha is not None and old_sha == new_sha:
            continue
        if kind == "file":
            if d.get("fp") not in (None, anchors.ABSENT) and d["fp"] == new_sha:
                continue
            change = "removed" if new_sha is None else ("added" if old_sha is None else "modified")
            out.append({"dep": d["dep_key"], "facet": d["facet"], "change": change, "file": path})
            continue
        if new_sha is None:
            out.append({"dep": d["dep_key"], "facet": d["facet"], "change": "removed", "file": path})
            continue
        f = _new_facts(store, repo, path, new_sha)
        if not anchors.usable(f) or f.get("scheme") != d.get("scheme"):
            out.append({"dep": d["dep_key"], "facet": d["facet"], "change": "modified", "file": path,
                        "why": "no comparable facts for the new version (file-level fallback)"})
            continue
        fp = anchors.facet_fp(f, d["dep_key"], d["facet"])
        if fp != d.get("fp"):
            out.append({"dep": d["dep_key"], "facet": d["facet"], "file": path,
                        "change": "gone" if fp == anchors.ABSENT else "changed"})
    return out


def _evidence_changes(repo: Path | None, evs: list[dict], changed_paths: set[str]) -> tuple[list[dict], list]:
    """Supporting file evidence in changed (or untracked) paths that no longer relocates to identical text."""
    bad: list[dict] = []
    moved: list = []
    if repo is None:
        return bad, moved
    for e in evs:
        if e["relation"] != "supports" or e["source_type"] not in RECHECK_TYPES or not e.get("path") \
                or not e.get("line_start") or (e.get("meta") or {}).get("root"):
            continue
        if e["path"] not in changed_paths:
            continue
        chk = evmod.check_source(repo, e)
        if chk.ok:
            if chk.status == "moved":
                moved.append((e, chk))
        else:
            bad.append({"file": e["path"], "change": "removed" if chk.status == "gone" else "modified",
                        "evidence": e["id"], "why": chk.reason})
    return bad, moved


# -- stale invalidation --------------------------------------------------------

ACTIVE = ("observed", "experiment_verified", "statically_verified", "primary_source_verified",
          "strong_inference", "weak_inference", "unknown")


def claim_files(store: Store, claim: dict) -> set[str]:
    files = set()
    for e in store.claim_evidence(claim["id"]):
        if e.get("path") and not (e.get("meta") or {}).get("root"):
            files.add(e["path"])
    for s in claim.get("subjects") or []:
        path = str(s).split("::")[0]
        if "/" in path or Path(path).suffix:
            files.add(path)
    return files


def watches_tests(claim: dict) -> bool:
    return (claim.get("spec") or {}).get("watch") == "tests"


def changed_claim_files(store: Store, claim: dict, old_files: dict[str, str],
                        new_files: dict[str, str]) -> list[dict]:
    """``[{"file", "change"}]`` for the files behind ``claim`` that differ between two file maps.

    The file-level rule (used for claims without recorded dependencies). A
    file the claim cites counts when it was modified or removed (a file that
    did not exist in the claim's snapshot is ignored). A claim whose spec has
    ``{"watch": "tests"}`` (e.g. "no test reaches X") also depends on the set
    of test files, so any test file added, modified or removed counts too.
    """
    touched: dict[str, str] = {}
    for f in sorted(claim_files(store, claim)):
        if f in old_files and new_files.get(f) != old_files[f]:
            touched[f] = "removed" if f not in new_files else "modified"
    if watches_tests(claim):
        for f in sorted(set(old_files) | set(new_files)):
            if f in touched or not _is_test_file(f) or old_files.get(f) == new_files.get(f):
                continue
            touched[f] = "added" if f not in old_files else "removed" if f not in new_files else "modified"
    return [{"file": f, "change": ch} for f, ch in sorted(touched.items())]


def assess_change(store: Store, repo: Path | None, claim: dict, old_files: dict[str, str],
                  new_files: dict[str, str], *, partial: bool = False) -> dict:
    """Did anything this claim depends on change between two file maps?

    Returns ``{"stale", "changed" (files), "facets", "moved", "touched"}``.
    Claims with recorded dependencies use them plus an evidence re-check of
    cited lines in changed or untracked files; older claims use the file rule.
    """
    deps = store.claim_deps(claim["id"])
    evs = store.claim_evidence(claim["id"])
    files = claim_files(store, claim)
    changed_paths = {p for p in set(old_files) | set(new_files) if old_files.get(p) != new_files.get(p)}
    untracked = {e["path"] for e in evs if e.get("path") and e["path"] not in old_files
                 and e["path"] not in new_files and not (e.get("meta") or {}).get("root")}
    if not deps:
        changed = changed_claim_files(store, claim, old_files, new_files)
        bad, moved = _evidence_changes(repo, evs, untracked)
        changed += [{"file": b["file"], "change": b["change"]} for b in bad if b["file"] not in
                    {c["file"] for c in changed}]
        return {"stale": bool(changed), "changed": changed, "facets": [], "moved": [],
                "touched": sorted({c["file"] for c in changed})}
    facets = dep_changes(store, repo, deps, old_files, new_files, partial=partial)
    bad, moved = _evidence_changes(repo, evs, (changed_paths & files) | untracked)
    by_file: dict[str, str] = {}
    for f in facets:
        if f.get("file"):
            by_file.setdefault(f["file"], "removed" if f["change"] == "removed" else
                               "added" if f["change"] == "added" else "modified")
    for b in bad:
        by_file.setdefault(b["file"], b["change"])
    changed = [{"file": f, "change": ch} for f, ch in sorted(by_file.items())]
    return {"stale": bool(facets or bad), "changed": changed, "facets": facets + bad, "moved": moved,
            "touched": sorted((changed_paths & (files | {anchors.split_key(d["dep_key"])[1] for d in deps}))
                              | {b["file"] for b in bad})}


def invalidate_stale(store: Store, new_snapshot: dict | None, *, actor: str = "verinoda",
                     files: dict[str, str] | None = None, repo: Path | None = None,
                     report: dict | None = None) -> list[dict]:
    """Mark claims stale when something they depend on changed since their snapshot.

    ``files`` (path -> sha256) compares against a working tree that has no
    recorded snapshot (e.g. the index refused to rebuild); ``new_snapshot``
    may then be None and nothing is re-bound. A claim whose files changed but
    whose dependencies and cited evidence did not is re-bound to
    ``new_snapshot`` without a status change (recorded in its history).
    ``report`` (optional) receives counts: checked, stale, rebound, ms.
    """
    import time

    t0 = time.perf_counter()
    new_files = files if files is not None else store.snapshot_files(new_snapshot["id"])
    new_sid = (new_snapshot or {}).get("id") if files is None else None
    new_commit = (new_snapshot or {}).get("commit_sha")
    if repo is None:
        root = (new_snapshot or {}).get("repo_root") or (store.latest_snapshot() or {}).get("repo_root")
        repo = Path(root) if root else None
    cache: dict[str, dict[str, str]] = {}
    changed_claims = []
    rebound = checked = 0
    cl = Claims(store, repo)
    for c in store.all(
        f"SELECT * FROM claims WHERE status IN ({','.join('?' * len(ACTIVE))}) AND superseded_by IS NULL",
        ACTIVE,
    ):
        sid = c["snapshot_id"]
        if not sid or sid == new_sid:
            continue
        old_files = cache.setdefault(sid, store.snapshot_files(sid))
        checked += 1
        res = assess_change(store, repo, c, old_files, new_files)
        commit_note = (f" (commit {c['commit_sha'][:10]} -> {(new_commit or '')[:10]})"
                       if c["commit_sha"] and new_commit and c["commit_sha"] != new_commit else "")
        if res["stale"]:
            n = len(res["changed"]) or len(res["facets"])
            what = ", ".join(sorted({f["dep"].partition(":")[2] + (f"#{f['facet']}" if f.get("facet") else "")
                                     for f in res["facets"] if f.get("dep")})[:4])
            cl.set_status(
                c["id"], "stale",
                reason=f"{n} file(s) changed since snapshot {sid}" + commit_note
                       + (f"; changed: {what}" if what else "")
                       + (" (working tree; no new snapshot recorded)" if files is not None else ""),
                actor=actor, payload={"changed": res["changed"], "facets": res["facets"][:20],
                                      "new_snapshot": new_sid},
            )
            changed_claims.append({"id": c["id"], "text": c["text"], "changed": res["changed"],
                                   "facets": res["facets"][:20]})
        elif res["touched"] and new_sid:
            for e, chk in res["moved"]:
                try:
                    evmod.record_location(store, e, chk, new_sid)
                except Exception:
                    pass
            extra = {"snapshot_id": new_sid, "commit_sha": new_commit}
            store.record_transition(
                c["id"], from_status=c["status"], to_status=c["status"], from_conf=c["confidence"],
                to_conf=c["confidence"], actor=actor,
                reason=f"rebound to snapshot {new_sid}{commit_note}: {len(res['touched'])} file(s) changed, "
                       "no dependency of this claim did",
                payload={"rebound_from": sid, "files": res["touched"][:20],
                         "moved": [{"evidence": e["id"], "to": list(chk.moved_to or [])} for e, chk in res["moved"]]},
                extra_fields={**extra, **({"verified_at": new_sid} if c["status"] in VERIFIED else {})})
            rebound += 1
    if report is not None:
        report.update(checked=checked, stale=len(changed_claims), rebound=rebound,
                      ms=round(1000 * (time.perf_counter() - t0), 1))
    return changed_claims
