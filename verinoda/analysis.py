"""Budgeted analysis loop: question -> plan -> claims with evidence -> critique -> answer.

Every analysis runs through a question plan (docs/DESIGN.md D1-D9,
:mod:`verinoda.question_plan`): the host agent's plan when one is given,
otherwise one drafted by rules. Order of work (cheapest reliable tool first):

 1. refresh the index when the working tree changed (errors become unknowns)
 2. plan: draft (or parse) and check it - schema, integrity, grounding,
    versions, mention links; the plan and its check are stored
    (``question_plans``) and the analysis references it (``analyses.plan_id``)
 3. an invalid plan stops here (``status: invalid_plan``); a plan that needs
    clarification with ``on_ambiguity: ask`` returns the grounded questions
    (``status: needs_clarification``) and writes no claim
 4. per sub-question, in dependency order, with a share of the budget:
    retrieval seeded by the linked mentions (and by the symbols located for
    ``subject_from``) with lexicon / seed-dictionary expansions; items must
    pass a relevance test against the question's grounded words, otherwise the
    sub-question is an ``unknown`` with a next step - never a claim about
    unrelated code
 5. location and relation claims for the relevant items, then the handler of
    the sub-question's intent (and of its secondary intents): flow, dataflow,
    callers, config, tests (optional test run / runtime observation), why /
    history, impact, behaviour (a proposition check), reference comparison
    (the reference is resolved offline; reading it is a next step)
 6. requested statuses follow the evidence rules: call sites are graded by
    :func:`verinoda.entail.call_site`; a relation's call site is resolved by
    :mod:`verinoda.precise` when it is installed (one budget per analysis):
    a definitive match supports it, a definitive mismatch refutes it, any other
    answer qualifies it and caps it at ``strong_inference``. With ``observe``
    the selected tests run under the call tracer (:mod:`verinoda.runtime`):
    observed calls support relation claims (never through test doubles) and
    the observed reach set supersedes a static tests claim it contradicts
 7. critique of each claim (confidence only goes down); a tests claim whose
    counter-probe refutes a listed test is superseded by a corrected claim
 8. each sub-question is judged against its ``done_when``: ``met``,
    ``met_with_inference``, ``unmet``, ``not_supported`` or
    ``blocked_by_clarification``; :func:`audit` recomputes the verdicts later
    and marks a sub-question ``stale`` when a claim it relies on went stale

A :class:`Budget` bounds wall time (index refresh and experiments included),
tool calls and returned context. Every claim, unknown, step and critique entry
that is returned is charged to the context budget at its serialised size
(tokens estimated as chars/4). When the budget runs out no further claim is
produced: every sub-question that was skipped or cut short is reported as an
``unknown`` naming it, instead of being answered from guesses.
"""

from __future__ import annotations

import fnmatch
import inspect
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from verinoda import anchors, callsite, entail, index, retrieval
from verinoda import architecture_map as am
from verinoda import evidence as evmod
from verinoda import question_plan as qp
from verinoda import textnorm as tn
from verinoda.claims import ORDER, VERIFIED, ClaimRuleError, Claims
from verinoda.paths import load_config
from verinoda.snapshot import current_state, git
from verinoda.store import Store, new_id, now

NEXT_BUDGET = "re-run with a larger --budget-* or a narrower question"
SUBQUESTIONS = {
    "location": "where is it defined?",
    "relations": "how are the retrieved symbols connected (calls/uses/inherits)?",
    "flow": "which path takes data from an entry point to persistence?",
    "callers": "which code calls it?",
    "config": "which configuration affects this?",
    "tests": "which tests statically reach it?",
    "why": "why is it built this way (decision records, history)?",
    "impact": "what does a change here affect?",
    "behaviour": "does the code behave as the question states?",
    "compare_reference": "how does it differ from the referenced version?",
}
# plan intent -> the legacy name kept in the ``intents`` output field
LEGACY_INTENT = {"locate": "location", "define": "location"}
LEGACY_ORDER = ("why", "flow", "dataflow", "config", "tests", "impact", "location", "callers", "behaviour",
                "history", "compare_reference", "performance", "architecture", "usage")
RELEVANCE_MIN = 0.25        # share of the grounded question words an item must carry
JAVA_BOUND = re.compile(r"\((imported|same package|fully qualified|imported with \*)\)$")
CALLERS_LISTED = 6          # calling functions turned into claims; the rest are named in an unknown
LOCATION_FIRST = {"locate", "define", "behaviour", "performance", "architecture", "usage"}
UNGROUNDED_MAJORITY = 0.5   # more than this share of content words absent from the repo -> unknown
MIN_STATUS_DEFAULT = "strong_inference"
PERSISTENCE_TARGET = "(persistence)"


@dataclass
class Budget:
    seconds: float = 60
    tool_calls: int = 40
    context_tokens: int = 6000
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    chars: int = 0
    exhausted_reason: str | None = None

    def _check(self) -> None:
        if self.exhausted_reason is not None:
            return
        if time.monotonic() - self.started > self.seconds:
            self.exhausted_reason = f"time budget {self.seconds}s spent"
        elif self.calls > self.tool_calls:
            self.exhausted_reason = f"tool-call budget {self.tool_calls} spent"
        elif self.chars // 4 > self.context_tokens:
            self.exhausted_reason = f"context budget ~{self.context_tokens} tokens spent"

    def spend(self, calls: int = 1, chars: int = 0) -> bool:
        self.calls += calls
        self.chars += chars
        self._check()
        return self.exhausted_reason is None

    def fits(self, chars: int) -> bool:
        """True when ``chars`` more returned context stay within budget (nothing is charged)."""
        self._check()
        if self.exhausted_reason is not None:
            return False
        if (self.chars + chars) // 4 > self.context_tokens:
            self.exhausted_reason = f"context budget ~{self.context_tokens} tokens spent"
            return False
        return True

    def work_ok(self) -> bool:
        """Time and one more tool call are still available (context aside)."""
        return time.monotonic() - self.started <= self.seconds and self.calls + 1 <= self.tool_calls

    def remaining_seconds(self) -> float:
        return self.seconds - (time.monotonic() - self.started)

    @property
    def ok(self) -> bool:
        self._check()
        return self.exhausted_reason is None

    def usage(self) -> dict:
        return {"elapsed_s": round(time.monotonic() - self.started, 3), "tool_calls": self.calls,
                "context_chars": self.chars, "context_tokens_est": self.chars // 4,
                "token_count_method": "chars/4 estimate", "exhausted": self.exhausted_reason}


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str))


# -- intents (legacy entry points, now backed by the plan's cue tables) -----------------------------

def fold_tr(text: str) -> str:
    """Lower-case, ASCII-folded Turkish (``İ``/``I``/``ı`` -> ``i``, ``ş`` -> ``s`` ...)."""
    return tn.fold_tr(tn.nfc(text)).replace("̇", "")


def _legacy(intents) -> list[str]:
    names = {LEGACY_INTENT.get(i, i) for i in intents}
    return [n for n in LEGACY_ORDER if n in names]


def turkish_intents(question: str) -> list[str]:
    """Intents found by the Turkish cue tables alone (legacy names)."""
    found = set()
    for cl in qp.segment(question):
        found.update(c["intent"] for c in qp.clause_cues(cl["text"]) if c["lang"] == "tr")
    return _legacy(found)


def intents_for(question: str, lexicon=None) -> list[str]:
    """Rule-based intents of a question in the legacy vocabulary (``location`` for locate/define)."""
    return _legacy(qp.intents_for(question, lexicon)) or ["location"]


# -- claims ---------------------------------------------------------------------------------------

class _Recorder:
    """Creates claims within the context budget and the current sub-question's share of it.

    A claim with the same text in the same snapshot is reused whatever its
    status (contradicted and stale included) - a repeated question never
    creates duplicates; reused claims are marked in the output.
    """

    def __init__(self, store: Store, repo: Path, snapshot: dict, analysis_id: str, budget: Budget,
                 extra_uncertainties: list[str] | None = None):
        self.store, self.repo, self.snap, self.aid, self.budget = store, repo, snapshot, analysis_id, budget
        self.cl = Claims(store, repo)
        self.extra = list(extra_uncertainties or [])
        self.ids: list[str] = []
        self.reused: set[str] = set()
        self.charged: dict[str, int] = {}
        self.section = "location"
        self.skipped: Counter = Counter()
        self.sq: str | None = None
        self.by_sq: dict[str, list[str]] = defaultdict(list)
        self.made_in: dict[str, set] = defaultdict(set)  # claim id -> {(sub-question, section)} that produced it
        self.sq_skipped: Counter = Counter()
        self.sq_cap: int | None = None
        self.sq_used = 0
        self.sq_extra: list[str] = []
        self.plan_ref: dict | None = None
        # uncertainties that come from this analysis' reading of the question (shown on reused claims too)
        self.context_unc: dict[str, list[str]] = defaultdict(list)
        # precise call-site resolution: one budget per analysis (None: not installed / not wanted)
        self.precise_budget = None
        try:  # evidence groups (claims.attach(..., grp=)) exist once the trust engine is installed
            self.groups_ok = "grp" in inspect.signature(Claims.attach).parameters
        except (TypeError, ValueError):
            self.groups_ok = False

    def begin(self, sq_id: str, cap_chars: int | None, plan_ref: dict | None, extra: list[str]) -> None:
        self.sq, self.sq_cap, self.sq_used, self.plan_ref, self.sq_extra = sq_id, cap_chars, 0, plan_ref, extra

    def _note(self, cid: str) -> None:
        self.made_in[cid].add((self.sq, self.section))
        if self.sq and cid not in self.by_sq[self.sq]:
            self.by_sq[self.sq].append(cid)
        for u in self.sq_extra:
            if u not in self.context_unc[cid]:
                self.context_unc[cid].append(u)

    def claim(self, text: str, *, kind: str, status: str, evidence: list[tuple],
              subjects: list[str], spec: dict | None = None, uncertainties: list[str] | None = None,
              supersedes: str | None = None) -> dict | None:
        unc = list(uncertainties or [])
        unc += [u for u in self.extra + self.sq_extra if u not in unc]
        prev = self.store.one(
            "SELECT id FROM claims WHERE text = ? AND snapshot_id = ? AND superseded_by IS NULL"
            " ORDER BY created_at LIMIT 1", (text, self.snap["id"]))
        if prev and prev["id"] in self.ids:
            self._note(prev["id"])
            return self.cl.get(prev["id"])
        est = _size({"id": "clm_000000000000", "text": text, "status": status, "confidence": 0.99,
                     "evidence": [f"supports:source_code:{(e[0] if isinstance(e[0], str) else e[0].get('locator'))}"
                                  for e in evidence if e[0]][:6], "uncertainties": unc, "reused": True})
        if not self.budget.fits(est) or (self.sq_cap is not None and self.sq_used + est > self.sq_cap):
            self.skipped[self.section] += 1
            self.sq_skipped[(self.sq, self.section)] += 1
            return None
        if prev:
            c = self.cl.get(prev["id"])
            self.reused.add(c["id"])
        else:
            if self.plan_ref:
                spec = {**(spec or {}), "plan": self.plan_ref}
            # (evidence, relation[, group]): the group is dropped where the claims module has none
            evs = [tuple(e) if self.groups_ok else tuple(e[:2]) for e in evidence if e[0]]
            c = self.cl.create(text, project=self.snap["project"], snapshot=self.snap, subjects=subjects,
                               status=status, evidence=evs, kind=kind, spec=spec,
                               uncertainties=unc, analysis_id=self.aid, supersedes=supersedes)
        self.ids.append(c["id"])
        self._note(c["id"])
        n = _size(self.record(c["id"]))
        self.charged[c["id"]] = n
        self.sq_used += n
        self.budget.spend(0, n)
        return c

    def record(self, cid: str, not_challenged: str | None = None) -> dict:
        c = self.cl.get(cid)
        unc = list(c["uncertainties"] or [])
        unc += [u for u in self.extra + self.context_unc.get(cid, []) if u not in unc]
        return {
            "id": cid, "text": c["text"], "status": c["status"], "confidence": round(c["confidence"], 2),
            "evidence": [f"{e['relation']}:{e['source_type']}:{e['locator']}" for e in self.cl.evidence(cid)][:6],
            "uncertainties": unc,
            **({"reused": True} if cid in self.reused else {}),
            **({"challenged": False, "not_challenged_reason": not_challenged} if not_challenged else {}),
        }


def _line_mentions(repo: Path, at: str | None, name: str) -> tuple[bool, str | None]:
    """Does the cited line name ``name`` (directly or through a Python import alias)?"""
    ok, matched, _ = callsite.check(repo, at, name)
    return ok, matched


def _src_ev(repo: Path, path: str, a: int, b: int | None, commit: str | None, **meta) -> dict | None:
    return evmod.source_evidence(repo, path, a, b, commit=commit, meta=meta or None)


# entail.call_site codes (Grade.code) that mean the cited line does not show the call at all; a
# precise "confirms" never overrides them (the claim's caller or module is wrong, not the binding)
_CALL_FAIL = {"outside_caller", "string_only", "absent", "wrong_module", "unreadable", "blank"}


def _caller_arg(label: str | None) -> str | None:
    """The caller name entail checks the call site against (None for a module/file node)."""
    s = (label or "").strip()
    if not s or (re.search(r"\.[A-Za-z]{1,5}$", s) and not s.endswith(")")):
        return None
    return s


_GRADES: dict[tuple, object] = {}   # call-site grades, keyed by the file's stat: flows share hop lines
_GRADES_MAX = 4096


def _call_site_grade(repo: Path, at: str | None, caller: str | None, target_label: str,
                     target_path: str | None, relation: str | None) -> entail.Grade | None:
    """entail's grade of the call site ``path:line`` (the grade the status rules will apply), or None."""
    if not at or ":" not in at:
        return None
    path, _, ln = at.rpartition(":")
    if not ln.isdigit():
        return None
    try:
        st = (Path(repo) / path).stat()
    except OSError:
        return None
    key = (str(repo), path, st.st_mtime_ns, st.st_size, int(ln), target_label, _caller_arg(caller), target_path,
           relation)
    if key in _GRADES:
        return _GRADES[key]
    try:
        grade = entail.call_site(repo, path, int(ln), target_label, caller=_caller_arg(caller),
                                 target_path=target_path, target_qual=target_label, relation=relation)
    except (OSError, ValueError, RecursionError):
        return None
    if len(_GRADES) >= _GRADES_MAX:
        _GRADES.clear()
    _GRADES[key] = grade
    return grade


def _precise_budget(repo: Path):
    """One :class:`verinoda.precise.Budget` per analysis, or None when no precise resolver is installed."""
    try:
        from verinoda import precise
    except Exception:  # noqa: BLE001 - optional module
        return None
    try:
        ok, _ = precise.available()
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        return None
    try:
        budget = precise.Budget.from_config(repo)
    except Exception:  # noqa: BLE001 - a bad config value falls back to the defaults
        budget = precise.Budget()
    # budget.precise_sites = 0 (or precise_seconds = 0) switches it off rather than skipping every site
    return budget if budget.max_sites > 0 and budget.max_seconds > 0 else None


def _precise(rec: _Recorder, at: str, target_label: str, target_path: str | None, target_line: int | None,
             commit: str | None) -> tuple[dict | None, dict | None, bool]:
    """``(result, evidence, skipped_for_budget)`` of a precise resolution of the call at ``at``."""
    pb = rec.precise_budget
    if pb is None:
        return None, None, False
    from verinoda import precise

    path, _, ln = at.rpartition(":")
    before = pb.skipped
    try:
        res = precise.resolve_call(rec.repo, path, int(ln), target_label, store=rec.store, budget=pb,
                                   target_path=target_path, target_line=target_line)
    except Exception:  # noqa: BLE001 - a resolver crash means "no precise answer"
        return None, None, False
    if res is None:
        return None, None, pb.skipped > before
    try:
        ev = precise.resolution_evidence(rec.repo, res, commit=commit)
    except Exception:  # noqa: BLE001
        ev = None
    if ev is not None:  # compact locator: the call line and the answer's kind (tool and targets are in meta)
        ev["locator"] = f"{res.get('path')}:{res.get('line')} {res.get('kind')}"
    return res, ev, False


def _disp(g: index.Graph, n: str) -> str:
    """A Java method's label with its class (``Wisp.spawn()``, not ``.spawn()``): Java labels
    methods without the class, and a mod has many ``spawn`` methods. Other labels are unchanged."""
    lab = g.label(n)
    if lab.startswith(".") and str(g.file(n) or "").endswith(".java"):
        cls = next((c for c, _ in g.in_edges(n, {"method"})), None)
        if cls is not None:
            return f"{g.label(cls).strip()}{lab}"
    return lab


def _edge_claim(rec: _Recorder, g: index.Graph, u: str, v: str, d: dict, commit: str | None) -> dict | None:
    at = retrieval._at(d)
    rel = d.get("relation")
    label = g.label(v)
    evs: list[tuple[dict, str]] = [(evmod.graph_edge_evidence({"source": u, "target": v, **d},
                                                               graph_path=str(g.path), commit=commit), "supports")]
    unc = []
    extracted = d.get("confidence") == "EXTRACTED"
    # what the status rules will say about the cited line (entail.call_site), not just a name match
    grade = _call_site_grade(rec.repo, at, g.label(u), label, g.file(v), rel)
    site_ev = None
    if at and grade is not None and grade.grade != "none":
        path, _, ln = at.rpartition(":")
        matched = _line_mentions(rec.repo, at, label)[1] if grade.code == "alias_call" else None
        site_ev = _src_ev(rec.repo, path, int(ln), None, commit, check="call_site",
                          **({"alias": matched} if matched else {}))
        evs.append((site_ev, "supports"))
        if matched:
            unc.append(f"{at} calls it through the import alias '{matched}'")
    # A Java `Cls.m(...)` graded full had its class resolved by the file's imports or package
    # against the target's file (entail._java_qualified_grade): which `m` is meant is settled.
    resolved = (grade.reason if grade is not None and grade.grade == "full" and grade.code == "call"
                and str(g.file(v) or "").endswith(".java") and JAVA_BOUND.search(grade.reason) else None)
    if grade is not None and grade.grade == "full":
        # The line calls the target, but for an INFERRED edge *which* symbol of
        # that name is meant is the extractor's inference - not verified.
        status = "statically_verified" if extracted or resolved else "strong_inference"
        if not extracted and not resolved:
            unc.append(f"{at} names '{label}', but resolving it to {g.file(v)} is INFERRED")
    elif grade is not None and grade.grade == "partial":
        status = "strong_inference"
        unc.append(grade.reason)  # the claim text already names the line
    else:
        status = "strong_inference" if extracted else "weak_inference"
        unc.append("call site line not confirmed to name the target"
                   + (f" ({grade.reason})" if grade is not None else ""))
    if at and rel == "calls":
        res, pev, skipped = _precise(rec, at, label, g.file(v), g.line(v), commit)
        verdict = (res or {}).get("verdict")
        tool = (res or {}).get("tool") or "resolver"
        if skipped:
            unc.append("precise resolution: not resolved (budget)")
        elif res is not None and pev is not None and verdict == "confirms":
            # the resolver's definitive answer is the evidence (listed with the claim), not an uncertainty
            evs.append((pev, "supports"))
            if grade is None or grade.code not in _CALL_FAIL:
                # the binding the line's grade left open (INFERRED target, local rebinding, alias) is settled
                status = "statically_verified"
                unc = [x for x in unc if "is INFERRED" not in x and (grade is None or x != grade.reason)]
        elif res is not None and pev is not None and verdict == "refutes":
            pev = {**pev, "meta": {**(pev.get("meta") or {}), "strength": "definitive"}}
            if site_ev is not None:  # the line names it, but the resolver binds the call elsewhere
                evs = [e for e in evs if e[0] is not site_ev]
            evs.append((pev, "refutes"))
            status = "contradicted"
            where = ", ".join(f"{t.get('path') or t.get('name')}:{t.get('line')}"
                              for t in (res.get("targets") or [])[:2])
            unc.append(f"{tool}: the call at {at} binds to {where or 'another definition'}, not "
                       f"{g.file(v)}:{g.line(v)}")
        elif res is not None and pev is not None:
            evs.append((pev, "qualifies"))
            capped = status in VERIFIED
            if capped:
                status = "strong_inference"
            # a dynamic answer on a line already graded partial says nothing new
            if capped or res.get("kind") != "dynamic" or grade is None or grade.grade == "full":
                why = str(res.get("reason") or "").split(":")[0][:90]
                unc.append(f"{tool}: {res.get('kind')}" + (f" ({why})" if why else ""))
    origin = str(d.get("_origin", ""))
    if origin.startswith("verinoda") and not resolved:
        how = "Java class and import resolution" if origin == index.JAVA_CALL_ORIGIN else "type-annotation resolution"
        unc.append(f"edge derived by {origin} ({how})")
    return rec.claim(
        f"`{_disp(g, u)}` {rel} `{_disp(g, v)}`" + (f" ({at})" if at else ""),
        kind="relation", status=status, evidence=evs,
        subjects=[f"{g.file(u)}::{g.label(u)}", f"{g.file(v)}::{label}"],
        spec={"source": u, "target": v, "target_label": label, "relation": rel, "at": at,
              "confidence": d.get("confidence"), "origin": d.get("_origin"),
              **({"resolved": resolved} if resolved else {})},
        uncertainties=unc,
    )


def _pytest_id(g: index.Graph, n: str) -> str:
    """``file::test`` or, for a method, ``file::Class::test`` (the class the method belongs to)."""
    name = g.label(n).strip().lstrip(".").split("(")[0]
    cls = next((u for u, _ in g.in_edges(n, {"method"}) if g.file(u) == g.file(n)), None)
    return f"{g.file(n)}::{g.label(cls).strip()}::{name}" if cls else f"{g.file(n)}::{name}"


# -- analysis context -------------------------------------------------------------------------------

@dataclass
class _Ctx:
    store: Store
    repo: Path
    g: index.Graph
    lex: object
    snap: dict
    commit: str | None
    budget: Budget
    rec: _Recorder
    step: object
    unknown: object
    cfg: dict
    plan: dict
    check: dict
    plan_id: str
    run_tests: bool
    observe: bool
    views: dict = field(default_factory=dict)
    located: dict = field(default_factory=dict)   # sub-question id -> {node: reason}
    started: set = field(default_factory=set)
    observations: list = field(default_factory=list)   # runtime observe() results of this analysis
    replaced: dict = field(default_factory=dict)       # claim id -> the claim that superseded it here

    def view(self, name: str):
        if name not in self.views:
            fn = {"dataflow": am.dataflow, "config": am.config, "tests": am.tests_view, "history": am.history}[name]
            self.views[name] = fn(self.g)
        return self.views[name]


@dataclass
class _Sub:
    """What one sub-question has to work with."""

    sq: dict
    links: list[dict]
    items: list[dict]          # relevant retrieved items
    prod: list[dict]           # relevant items outside test files
    seeds: dict
    words: list[str]           # grounded words and expansions used for relevance and ranking
    subject_nodes: dict
    flags: dict = field(default_factory=dict)
    unknowns: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def _begin(ctx: _Ctx, sub: _Sub, section: str) -> bool:
    """Start a handler section, or report it as unknown when the budget is gone."""
    ctx.rec.section = section
    if ctx.budget.ok:
        ctx.started.add((sub.sq["id"], section))
        return True
    _unknown(ctx, sub, {"question": SUBQUESTIONS.get(section, section), "why": ctx.budget.exhausted_reason,
                        "next_step": NEXT_BUDGET})
    return False


def _unknown(ctx: _Ctx, sub: _Sub | None, u: dict) -> None:
    if sub is not None:
        u = {**u, "sub_question": sub.sq["id"]}
        sub.unknowns.append(u)
    ctx.unknown(u)


# -- retrieval and relevance -----------------------------------------------------------------------

def _retrieve(ctx: _Ctx, inputs: dict, include_tests: bool) -> dict:
    """Call retrieval with seeds/expansions when it accepts them, else fold them into the query."""
    try:
        params = inspect.signature(retrieval.retrieve).parameters
    except (TypeError, ValueError):
        params = {}
    query = inputs["query"]
    kw: dict = {"include_tests": include_tests}
    if "seeds" in params:
        kw["seeds"] = inputs["seeds"] or None
    elif inputs["seeds"]:
        query += " " + " ".join(dict.fromkeys(qp._bare(ctx.g.label(n)) for n in inputs["seeds"]
                                              if n in ctx.g.G and ctx.g.is_symbol(n)))
    exp_terms = [t for v in inputs["expansions"].values() for t in v]
    if "expansions" in params:
        kw["expansions"] = inputs["expansions"] or None
    elif exp_terms:
        query += " " + " ".join(dict.fromkeys(exp_terms))
    rb = getattr(retrieval, "Budget", None)
    budget = rb(max_items=10, max_chars=6000) if rb else None
    return retrieval.retrieve(ctx.g, query, budget, **kw)


PASSAGE_CHARS = 6000  # the budget `verinoda query` renders its passages with


def _passages(g, question: str) -> list[str]:
    """What `verinoda query` gives for the same question (its text, to the same budget), line by line
    (in JSON a list of lines reads as the text does; one string would carry every line break escaped).

    Claims hold what was verified; these are the passages the claims were chosen from and the
    ones no claim covers, so an answer built on an analysis never has less to go on than a search
    (on the seven public benchmark sets analyze had lost 62 gold facts query found, over 33 of 74
    questions)."""
    try:
        res = retrieval.retrieve(g, question, retrieval.Budget(max_items=10, max_chars=PASSAGE_CHARS))
        return retrieval.render_text(res, PASSAGE_CHARS).splitlines()
    except Exception:  # noqa: BLE001 - passages are an addition; the claims stand without them
        return []


def _content_words(text: str) -> list[str]:
    out = []
    for w in tn.words(text):
        if w not in out and not w.isdigit():
            out.append(w)
    return out


def _hay(item: dict) -> str:
    """Text an item offers for relevance: its name, its file's name and its excerpt.

    Directory names are left out - in ``orders/`` every file would carry "order".
    """
    base = Path(item.get("file") or "").name
    return tn.fold_tr(" ".join([item.get("symbol") or "", base, item.get("excerpt") or ""]))


def _has_term(hay: str, term: str) -> bool:
    t = tn.en_stem(tn.fold_tr(term))
    if len(t) < 3 and t not in tn.SHORT_TECH:
        return False
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(t) if len(t) < 4 else re.escape(t), hay))


def _anchor_groups(ctx: _Ctx, sub: _Sub, expansions: dict) -> list[tuple[float, list[str]]]:
    """Weighted word groups an item must carry to be relevant (grounded mentions, subjects)."""
    groups: list[tuple[float, list[str]]] = []
    mentions = {m["id"]: m for m in ctx.plan.get("mentions") or []}
    for lk in sub.links:
        if lk["status"] not in ("linked", "weak") or not lk.get("nodes") and not lk.get("family_merged"):
            continue
        m = mentions.get(lk["mention"], {})
        words = [w for w in qp._surface_words(m.get("text") or "") if len(w) >= 2]
        terms = list(words)
        for w in words:
            terms += expansions.get(w, [])
        for k, v in expansions.items():
            if any(w in k.split() for w in words):
                terms += v
        terms += [w for w in re.findall(r"[a-z0-9_]+", (m.get("gloss_en") or "").lower())]
        for n in qp.mention_nodes(lk)[:3]:
            terms += [p for p in tn.split_identifier(qp._bare(ctx.g.label(n))) if len(p) >= 3][:3]
        terms = list(dict.fromkeys(t for t in terms if t))
        present = [t for t in terms if ctx.lex is not None and ctx.lex.has(t)]
        weight = max((ctx.lex.idf(t) for t in present), default=1.0) if ctx.lex is not None else 1.0
        groups.append((weight, terms))
    for n in list(sub.subject_nodes)[:5]:
        parts = [p for p in tn.split_identifier(qp._bare(ctx.g.label(n))) if len(p) >= 3]
        if parts:
            groups.append((1.0, parts))
    return groups


def _relevance(item: dict, groups: list[tuple[float, list[str]]]) -> float:
    if not groups:
        return 0.0
    hay = _hay(item)
    tot = sum(w for w, _ in groups) or 1.0
    got = sum(w for w, terms in groups if any(_has_term(hay, t) for t in terms))
    return got / tot


def _ungrounded(ctx: _Ctx, sq: dict, links: list[dict], expansions: dict) -> tuple[list[str], list[str]]:
    """Content words of the sub-question that the repository does not contain (nor any expansion)."""
    text = sq.get("text_user_lang") or sq.get("text") or ""
    cue_words = set()
    for c in qp.clause_cues(text, ctx.lex):
        cue_words.update(re.findall(r"[a-z]+", tn.fold_tr(c["cue"]).lower()))
    words = [w for w in _content_words(text) if w not in cue_words and w not in qp._GENERIC_EN and len(w) >= 4
             and not w.startswith(qp._GENERIC_TR_PREFIX)]
    linked_words = set()
    for lk in links:
        # an ambiguous mention names several things that exist here: its words occur
        if lk["status"] in ("linked", "weak", "ambiguous"):
            linked_words.update(qp._surface_words(lk.get("text") or ""))
    missing = []
    for w in words:
        if w in linked_words or (ctx.lex is not None and ctx.lex.has(w)):
            continue
        if any(w in k.split() and v for k, v in expansions.items()):
            continue
        missing.append(w)
    return words, missing


# -- the claims every sub-question gets: where its code is, how it connects --------------------------

def _exact_span(ctx: _Ctx, nid: str, path: str, name: str, sp: tuple[int, int]) -> tuple[int, int, str | None]:
    """``(start, end, note)`` of a definition. The end line comes from the file's syntax facts
    (:mod:`verinoda.anchors`, what the location grader checks) when they know the definition,
    else from the graph's span, which is exact only for a Python AST end line; ``note`` is
    None for an exact span and says why otherwise (such a claim is at most an inference)."""
    a, b = int(sp[0]), int(sp[1])
    try:
        facts = anchors.facts_for(ctx.store, ctx.repo, path)
    except Exception:  # noqa: BLE001 - no facts: fall back to the graph's span basis
        facts = None
    if anchors.usable(facts):
        clean = (name or "").strip().strip("`").strip()
        if facts.get("lang") == "markdown":
            for s in (facts.get("sections") or {}).values():
                if s["start"] == a and entail.norm_title(s["title"]) == entail.norm_title(clean):
                    return a, int(s["end"]), None
        else:
            for _, s in anchors.symbols_named(facts, clean):
                if a in (s["start"], s["def"]):
                    return a, int(s["end"]), None
    basis = getattr(ctx.g, "span_basis", lambda _n: None)(nid) or "heuristic"
    if basis == "ast" and path.endswith((".py", ".pyi")):
        return a, b, None
    if basis == "heuristic":
        return a, b, f"end line {b} is a heuristic (next definition); the definition may end earlier"
    return a, b, f"end line {b} ({basis}) is not confirmed by syntax facts for this file"


LINK_CHAIN_CLAIMS = 3   # relation claims around code that names a data resource (per analysis step)


def search_index_data_suffixes() -> tuple[str, ...]:
    from verinoda import resources

    return resources.DATA_REF_SUFFIXES + (".yml", ".yaml", ".toml")


def _link_chain(ctx: _Ctx, src: str, ln: int, done: int) -> int:
    """The code path around a line that names a data resource: the call made on that line
    (``Datapack.run(...)`` - what actually loads it) and the functions that call the naming
    function (how the game reaches it). Returns the number of claims made."""
    g = ctx.g
    fn = g.symbol_at(src, ln)
    if fn is None or done >= LINK_CHAIN_CLAIMS:
        return 0
    made = 0
    here = [(u, v, d) for u, v, d in g.G.out_edges(fn, data=True)
            if d.get("relation") == "calls" and d.get("source_location") == f"L{ln}"]
    callers = sorted(g.in_edges(fn, {"calls"}), key=lambda x: (x[1].get("confidence") != "EXTRACTED",
                                                                am.is_test_file(g.file(x[0])), g.file(x[0]) or ""))
    for u, v, d in here[:1] + [(c, fn, d) for c, d in callers[:2]]:
        if done + made >= LINK_CHAIN_CLAIMS or not ctx.budget.ok:
            break
        if _edge_claim(ctx.rec, g, u, v, d, ctx.commit) is not None:
            made += 1
    return made


def _unit_name(ctx: _Ctx, file: str, line: int) -> str:
    """The search index's name for the unit holding ``file:line`` (one label source for both link ends)."""
    from verinoda import search_index

    try:
        h = search_index.open_for(ctx.g, sync=False)
    except Exception:  # noqa: BLE001 - the name is cosmetic; the file stands in
        return ""
    u = search_index.unit_at(h, file, line)
    return h.units[u][3] if u is not None else ""


def _best_line(repo: Path, it: dict, words: list[str]) -> tuple[int, str] | None:
    """The line of an item's window that carries the most question words (else its first non-blank line)."""
    a, b = it["lines"]
    lines = index.file_lines(repo / it["file"]) or []
    stems = [tn.fold_tr(w).lower()[:5] for w in words if len(w) >= 3]
    best = None
    for n in range(a, min(b, len(lines)) + 1):
        s = lines[n - 1].strip()
        if not s:
            continue
        low = tn.fold_tr(s).lower()
        hits = sum(1 for w in stems if w in low)
        if best is None or hits > best[0]:
            best = (hits, n, s)
    return (best[1], best[2]) if best else None


def _data_link_claims(ctx: _Ctx, sub: _Sub, raw_items: list[dict]) -> None:
    """Claims for data-file hits (``file:line contains: ...``) and for resource links between code and data.

    A link claim quotes the line that names the id (verified verbatim); that the id is the other
    file is the resource-location rule, and for a bare name also an assumed namespace (uncertain).
    """
    rec, repo, commit = ctx.rec, ctx.repo, ctx.commit
    data_items = [i for i in sub.items if i.get("kind") == "data"]
    if sub.sq.get("intent") == "config":
        # "where is this setting?": the config file's line is the answer even when code outranks it
        from verinoda.search_index import CONFIG_SUFFIXES

        data_items += [i for i in raw_items[:retrieval.OUTLINE_ITEMS * 3] if i.get("kind") == "data"
                       and i["file"].lower().endswith(CONFIG_SUFFIXES) and i not in data_items]
    data_items = data_items[:2]
    linked = [i for i in raw_items[:retrieval.OUTLINE_ITEMS] if i.get("links")]
    if not (data_items or linked) or not _begin(ctx, sub, "location"):
        return
    for it in data_items:
        found = _best_line(repo, it, sub.words)
        if not found or not ctx.budget.ok:
            continue
        ln, text = found
        rec.claim(f"{it['file']}:{ln} contains: {text[:140]}", kind="location", status="statically_verified",
                  evidence=[(_src_ev(repo, it["file"], ln, ln, commit), "supports")], subjects=[it["file"]])
    made = chained = 0
    seen: set[tuple] = set()
    for it in linked:
        pairs = [(e["file"], e["line"], e["unit"], e["rid"], e["form"], e.get("sure", True), it["file"])
                 for e in it["links"].get("named_by", [])[:2]]
        for e in it["links"].get("names", []):
            # retrieval lists a target only when it is itself a ranked candidate
            tgt = next((t for t in e["targets"] if t != it["file"]), None)
            if tgt:
                pairs.append((it["file"], e["line"], "", e["rid"], e["form"], e.get("sure", True), tgt))
        for src, ln, unit, rid, form, sure, target in pairs:
            if made >= 3 or not ctx.budget.ok:
                break
            if (src, ln, rid, target) in seen:  # the same link seen from both of its ends
                continue
            seen.add((src, ln, rid, target))
            lines = index.file_lines(repo / src) or []
            if not 0 < ln <= len(lines):
                continue
            unit = unit or _unit_name(ctx, src, ln)
            unc = []
            if form == "bare":
                unc.append(f"`{rid}` is a bare name: it was matched to {target} by file name, the namespace is "
                           "not written on the line")
            elif not sure:
                unc.append(f"the line does not say which kind of resource `{rid}` is; {target} is one of several "
                           "files with this id")
            if rec.claim(f"`{unit or src}` names `{rid}` ({target}); {src}:{ln} contains: {lines[ln - 1].strip()[:140]}",
                         kind="general", status="strong_inference" if unc else "statically_verified",
                         evidence=[(_src_ev(repo, src, ln, ln, commit), "supports")],
                         subjects=[src, target], spec={"rid": rid, "form": form, "target": target},
                         uncertainties=unc) is not None:
                made += 1
                if src != target and not src.lower().endswith(search_index_data_suffixes()):
                    chained += _link_chain(ctx, src, ln, chained)


ON_SUBJECT_RELATIONS = {"method", "contains"}
CONTEXT_SECTIONS = {"location", "relations"}  # _context_claims: what the search found around the question


def _on_subject(ctx: _Ctx, sub: _Sub, it: dict) -> bool:
    """Is a retrieved item what the sub-question is about, rather than something ranked near it?

    A symbol the question names or links (or a member / the owner of one), or an item that carries
    every group of the question's grounded words. Location claims made for other items are context:
    they never make the sub-question met on their own (a verified definition of the wrong function
    is still the wrong answer)."""
    g, anchored = ctx.g, sub.extra.get("anchored") or set()
    nid = it.get("id")
    if nid in anchored:
        return True
    if nid in g.G:
        near = {u for u, _ in g.in_edges(nid, ON_SUBJECT_RELATIONS)} | {v for v, _ in g.out_edges(nid, ON_SUBJECT_RELATIONS)}
        if near & anchored:
            return True
    elif any(n in g.G and g.file(n) == it.get("file") and qp._bare(g.label(n)) in (it.get("excerpt") or "")
             for n in anchored):
        return True  # a module-level block that holds a named symbol
    groups = sub.extra.get("groups") or []
    return bool(groups) and len(groups) >= 2 and _relevance(it, groups) >= 1.0


def _mark_context(ctx: _Ctx, sub: _Sub, it: dict, c: dict | None) -> None:
    if c is not None and not _on_subject(ctx, sub, it):
        off = sub.flags.setdefault("off_subject", [])
        if c["id"] not in off:
            off.append(c["id"])


def _context_claims(ctx: _Ctx, sub: _Sub, raw_items: list[dict]) -> None:
    g, rec, commit, repo = ctx.g, ctx.rec, ctx.commit, ctx.repo
    items = sub.prod or sub.items
    module_blocks = [i for i in sub.items if i["symbol"] == "(module level)"]
    graph_items = [i for i in items if i["id"] in g.G]
    if (graph_items or module_blocks) and _begin(ctx, sub, "location"):
        # 3 definitions when reading the code is the answer, 2 alongside a dedicated handler
        n_loc = 3 if sub.sq["intent"] in LOCATION_FIRST else 2
        for it in graph_items[:n_loc]:
            sp = g.span(it["id"]) or (tuple(it["span"]) if it.get("span") else None)
            if not sp:
                continue
            if not ctx.budget.ok:
                rec.skipped["location"] += 1
                continue
            a, b, inexact = _exact_span(ctx, it["id"], it["file"], it["symbol"], sp)
            made = rec.claim(f"`{it['symbol']}` is defined at {it['file']}:{a}-{b}", kind="location",
                             status="strong_inference" if inexact else "statically_verified",
                             evidence=[(_src_ev(repo, it["file"], a, b, commit, symbol=it["symbol"]), "supports")],
                             subjects=[f"{it['file']}::{it['symbol']}"], spec={"symbol": it["symbol"]},
                             uncertainties=[inexact] if inexact else [])
            _mark_context(ctx, sub, it, made)
            if made is not None:
                ctx.step("verify_source", f"{it['file']}:{a}-{b}")
                ctx.located.setdefault(sub.sq["id"], {})[it["id"]] = f"located for {sub.sq['id']}"
        for it in module_blocks[:2]:
            if not ctx.budget.ok:
                rec.skipped["location"] += 1
                continue
            a, b = it["lines"]
            first = next((ln.strip() for ln in (it["excerpt"] or "").splitlines() if ln.strip()), "")
            _mark_context(ctx, sub, it, rec.claim(f"{it['file']}:{a}" + (f"-{b}" if b != a else "") + f" contains: {first[:120]}",
                                                  kind="location", status="statically_verified",
                                                  evidence=[(_src_ev(repo, it["file"], a, b, commit), "supports")],
                                                  subjects=[it["file"]]))
    _data_link_claims(ctx, sub, raw_items)
    # relation claims among the retrieved items, at least one end relevant (cheap and useful)
    relevant = {i["id"] for i in sub.items}
    chosen = {i["id"]: i["score"] for i in raw_items if i["id"] in g.G}
    rel_edges = sorted(((u, v, d) for u in chosen for v, d in g.out_edges(u, {"calls", "inherits", "uses"})
                        if v in chosen and (u in relevant or v in relevant)),
                       key=lambda x: (-(chosen[x[0]] + chosen[x[1]]), x[0], x[1], retrieval._at(x[2]) or ""))
    if rel_edges and _begin(ctx, sub, "relations"):
        for u, v, d in rel_edges[:6]:
            if not ctx.budget.ok:
                rec.skipped["relations"] += 1
                continue
            if _edge_claim(rec, g, u, v, d, commit) is not None:
                ctx.step("verify_call_site", f"{g.label(u)} -> {g.label(v)}")


# -- handlers ----------------------------------------------------------------------------------------

def _path_claim(ctx: _Ctx, hops: list[dict], text: str, *, sink_lines: list[str] = (), extra_spec: dict | None = None,
                subjects: list[str], base_unc: list[str]) -> dict | None:
    # hop and sink lines form one evidence group: the flow is verified only when every hop is
    evs: list[tuple[dict, str, str]] = []
    confirmed, inferred, aliased, weak = True, False, [], []
    for h in hops:
        extracted = h["confidence"] == "EXTRACTED"
        inferred |= not extracted
        path, _, ln = (h["at"] or "").rpartition(":")
        if not (path and ln.isdigit()):  # an edge without a line locator cannot be confirmed
            confirmed = False
            continue
        # the grade the flow grader applies to this hop line (entail call-site codes)
        gr = _call_site_grade(ctx.repo, h["at"], entail._token(h["from"]) or None, h["to"], None, h["relation"])
        full = gr is not None and gr.grade == "full"
        confirmed &= full
        if gr is not None and gr.code == "alias_call":
            matched = _line_mentions(ctx.repo, h["at"], h["to"])[1]
            if matched:
                aliased.append(f"{h['at']} calls {h['to']} as '{matched}'")
        if not full and extracted and gr is not None and len(weak) < 2:
            weak.append(f"{h['at']}: {gr.reason}")
        evs.append((_src_ev(ctx.repo, path, int(ln), None, ctx.commit, hop=f"{h['from']}->{h['to']}"), "supports",
                    "flow"))
    for sl in list(sink_lines)[:2]:
        path, _, ln = sl.rpartition(":")
        if path and ln.isdigit():
            evs.append((_src_ev(ctx.repo, path, int(ln), None, ctx.commit, sink=True), "supports", "flow"))
    status = "statically_verified" if confirmed and not inferred else "strong_inference"
    unc = list(base_unc)
    if inferred:
        unc.append("at least one hop is INFERRED")
    unc += aliased + weak
    return ctx.rec.claim(text, kind="flow", status=status, evidence=evs, subjects=subjects,
                         spec={"hops": [{k: h.get(k) for k in ("from", "to", "relation", "confidence", "at")}
                                        for h in hops], **(extra_spec or {})},
                         uncertainties=unc)


def _role_nodes(ctx: _Ctx, sub: _Sub, role: str) -> tuple[list[str], str | None]:
    """Nodes of the sub-question's source/target mention (``PERSISTENCE_TARGET`` for a storage word)."""
    mentions = {m["id"]: m for m in ctx.plan.get("mentions") or []}
    links = {lk["mention"]: lk for lk in sub.links}
    ids = [mid for mid in sub.sq.get("mentions") or [] if mentions.get(mid, {}).get("role") == role]
    subjects = (sub.sq.get("done_when") or {}).get("subjects") or []
    msubj = [s for s in subjects if s.startswith("m")]
    host_plan = not str(ctx.plan.get("derived_by") or "").startswith(qp.DRAFT_RULES)
    if not ids and host_plan and sub.sq["intent"] == "flow" and len(msubj) >= 2:
        # the schema's convention for path_found: source first, target last
        ids = [msubj[0]] if role == "source" else [msubj[-1]]
    nodes: list[str] = []
    texts: list[str] = []
    for mid in ids:
        m = mentions.get(mid, {})
        storage = qp.is_persistence_word(m.get("text", "") + " " + (m.get("gloss_en") or ""))
        if role == "target" and storage:
            return [], PERSISTENCE_TARGET
        if role == "source" and storage:
            continue  # "loads it from storage": storage is where the path ends, not an entry point
        lk = links.get(mid)
        if not lk or lk["status"] not in ("linked", "weak"):
            continue
        texts.append(m.get("text") or "")
        for n in qp.mention_nodes(lk)[:3]:
            # a file stands for the symbols defined in it
            for x in ([n] if ctx.g.is_symbol(n) else ctx.g.symbols_in(ctx.g.file(n) or "")[:5]):
                if x not in nodes:
                    nodes.append(x)
    return nodes[:8], " ".join(texts) or None


def _h_flow(ctx: _Ctx, sub: _Sub) -> None:
    g = ctx.g
    if not _begin(ctx, sub, "flow"):
        return
    src, src_text = _role_nodes(ctx, sub, "source")
    tgt, tgt_text = _role_nodes(ctx, sub, "target")
    if src and tgt:
        found = []
        for s in src:
            for t in tgt:
                if s == t:
                    continue
                tr = retrieval.trace(g, s, t)
                ctx.step("trace", f"{g.label(s)} -> {g.label(t)}: {tr['status']}")
                found += tr.get("paths") or []
                if len(found) >= 3:
                    break
        sinks = {s["at"]: s for s in ctx.view("dataflow")["sinks"]} if found else {}
        for hops in found[:3]:
            if not ctx.budget.ok:
                ctx.rec.skipped["flow"] += 1
                continue
            chain = " -> ".join([hops[0]["from"]] + [h["to"] for h in hops])
            end = hops[-1]["to_id"]
            sink = sinks.get(f"{g.file(end)}:{g.line(end)}")  # the path ends where data is written/read
            kinds = sorted({e["kind"] for e in (sink or {}).get("evidence", [])})
            _path_claim(ctx, hops, f"Call path {chain}"
                        + (f" reaches persistence ({', '.join(kinds)}) at {sink['at']}" if sink else ""),
                        sink_lines=[e["at"] for e in (sink or {}).get("evidence", [])],
                        extra_spec={"sink_kinds": kinds} if sink else None,
                        subjects=[g.file(hops[0]["from_id"]) or "", g.file(end) or ""],
                        base_unc=[f"endpoints linked from the question words '{src_text}' and '{tgt_text}'"])
        if not found:
            sub.flags["no_path"] = True
            _unknown(ctx, sub, {
                "question": sub.sq.get("text") or SUBQUESTIONS["flow"],
                "why": f"no directed call path from {', '.join(g.label(s) for s in src)} to "
                       f"{', '.join(g.label(t) for t in tgt)} in the graph",
                "next_step": f"`verinoda trace {qp._bare(g.label(src[0]))} {qp._bare(g.label(tgt[0]))} --mode any` "
                             "for non-call links, or name the intermediate function"})
        return
    _dataflow_paths(ctx, sub, src or _named_symbols(ctx, sub))


def _h_dataflow(ctx: _Ctx, sub: _Sub) -> None:
    if not _begin(ctx, sub, "flow"):
        return
    src, _ = _role_nodes(ctx, sub, "source")
    _dataflow_paths(ctx, sub, src or _named_symbols(ctx, sub))


def _named_symbols(ctx: _Ctx, sub: _Sub) -> list[str]:
    """Symbols the question spells as code (seeds that are not carried subjects)."""
    return [n for n in sub.seeds if n in ctx.g.G and ctx.g.is_symbol(n) and n not in sub.subject_nodes][:5]


def _dataflow_paths(ctx: _Ctx, sub: _Sub, src: list[str]) -> None:
    """Entry -> persistence paths that touch this sub-question's code (never unrelated ones)."""
    g = ctx.g
    df = ctx.view("dataflow")
    ctx.step("dataflow_view", f"{len(df['paths'])} entry->sink paths")
    anchor = {i["id"] for i in sub.items if i["id"] in g.G} | set(src) | set(sub.seeds) | set(sub.subject_nodes)
    locs = {f"{g.file(n)}:{g.line(n)}" for n in anchor if n in g.G}
    labels = {g.label(n) for n in anchor if n in g.G}
    if src:  # the question names where the data comes from: the path must pass through it
        src_locs = {f"{g.file(n)}:{g.line(n)}" for n in src}
        src_labels = {g.label(n) for n in src}
        touched = [p for p in df["paths"] if p["entry"] in src_locs
                   or (p["hops"] and p["hops"][0]["from"] in src_labels)]
        touched = touched or [p for p in df["paths"] if any(h["from"] in src_labels or h["to"] in src_labels
                                                             for h in p["hops"])]
    else:  # paths through the code the question is about
        touched = [p for p in df["paths"] if p["entry"] in locs or p["sink"] in locs
                   or any(h["from"] in labels or h["to"] in labels for h in p["hops"])]
    made = 0
    for p in touched[:3]:
        if not ctx.budget.ok:
            ctx.rec.skipped["flow"] += 1
            continue
        if p["hops"]:
            chain = " -> ".join([p["hops"][0]["from"]] + [h["to"] for h in p["hops"]])
        else:  # the entry point itself writes to the sink
            chain = next((e["symbol"] for e in df["entries"] if e["at"] == p["entry"]), p["entry"])
        if _path_claim(ctx, p["hops"], f"Data path {chain} reaches persistence ({', '.join(p['sink_kinds'])}) "
                                       f"at {p['sink']}", sink_lines=p["sink_lines"],
                       extra_spec={"sink_kinds": p["sink_kinds"]},
                       subjects=[p["entry"].rpartition(":")[0], p["sink"].rpartition(":")[0]],
                       base_unc=["entry point and sink detection are heuristics"]) is not None:
            ctx.step("verify_path", chain)
            made += 1
        _sink_claims(ctx, p)
    if not touched:
        sub.flags["no_path"] = True
        why = ("no entry->sink path found over call edges" if not df["paths"] else
               f"none of the {len(df['paths'])} entry->sink paths passes through the code this question is about")
        _unknown(ctx, sub, {"question": SUBQUESTIONS["flow"], "why": why,
                            "next_step": "name the entry function and the storage function: `verinoda trace A B`"})


def _sink_claims(ctx: _Ctx, p: dict) -> None:
    """Where a data path touches storage, as read in the source: ``file:line`` and the line itself.

    The path claim says the data gets there; these say where "there" is, which is what a
    question like "where is an order written to the database?" asks for."""
    kinds = ", ".join(p.get("sink_kinds") or [])
    for sl in list(p.get("sink_lines") or [])[:2]:
        path, _, ln = sl.rpartition(":")
        if not (path and ln.isdigit()) or not ctx.budget.ok:
            continue
        try:
            lines = (ctx.repo / path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        n = int(ln)
        if not 1 <= n <= len(lines) or not lines[n - 1].strip():
            continue
        ctx.rec.claim(f"{path}:{n} touches storage ({kinds}): {lines[n - 1].strip()[:140]}", kind="location",
                      status="statically_verified",
                      evidence=[(_src_ev(ctx.repo, path, n, n, ctx.commit, sink=True), "supports")], subjects=[path],
                      uncertainties=["which storage operation a line is comes from a pattern over the source"])


def _targets(ctx: _Ctx, sub: _Sub, n: int = 2) -> list[str]:
    """Symbols a sub-question is about: name-linked mentions, then carried subjects, then relevant items."""
    g = ctx.g

    def code(nid: str) -> bool:
        f = g.file(nid) or ""
        return g.is_symbol(nid) and Path(f).suffix.lower() in _CODE_SUFFIXES

    out = [nid for nid in sub.seeds if nid in g.G and code(nid)]
    if out:  # "who calls place_order?": the callers of place_order, not of whatever ranked next to it
        return out[:n]
    for it in sub.prod:
        if it["id"] in g.G and it["id"] not in out and code(it["id"]):
            out.append(it["id"])
    return out[:n]


_CODE_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".scala", ".rb", ".php",
                  ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".swift", ".lua", ".sh", ".ex", ".exs", ".jl", ".zig",
                  ".m", ".groovy", ".dart", ".mjs", ".cjs", ".ps1"}


def _h_callers(ctx: _Ctx, sub: _Sub) -> None:
    g = ctx.g
    if not _begin(ctx, sub, "callers"):
        return
    for t in _targets(ctx, sub):
        # extracted edges first, product code before tests, one claim per calling function
        callers = sorted(g.in_edges(t, {"calls"}), key=lambda x: (x[1].get("confidence") != "EXTRACTED",
                                                                   am.is_test_file(g.file(x[0])),
                                                                   g.file(x[0]) or "", x[0]))
        seen_callers: set[str] = set()
        callers = [c for c in callers if not (c[0] in seen_callers or seen_callers.add(c[0]))]
        ctx.step("callers", f"{g.label(t)}: {len(callers)} calling function(s)")
        if not callers:
            sub.flags.setdefault("empty_sets", []).append(g.label(t))
            _unknown(ctx, sub, {"question": f"which code calls {g.label(t)}?",
                                "why": "no call edge into it in the graph (dynamic, reflective or external callers "
                                       "are not extracted)",
                                "next_step": f"search for '{callsite.target_token(g.label(t))}(' in the code, or "
                                             "observe it at runtime"})
            continue
        for u, d in callers[:CALLERS_LISTED]:
            if not ctx.budget.ok:
                ctx.rec.skipped["callers"] += 1
                continue
            _edge_claim(ctx.rec, g, u, t, d, ctx.commit)
        rest = callers[CALLERS_LISTED:]
        if rest:
            more = ", ".join(f"{g.label(u)} ({g.file(u) or '?'})" for u, _ in rest[:4])
            _unknown(ctx, sub, {"question": f"which other code calls {g.label(t)}?",
                                "why": f"{len(rest)} more calling function(s) in the graph were not turned into "
                                       f"claims (the list is capped at {CALLERS_LISTED}): {more}"
                                       + (", ..." if len(rest) > 4 else ""),
                                "next_step": f"ask about one of them, or search for "
                                             f"'{callsite.target_token(g.label(t))}(' in the code"})


def _h_config(ctx: _Ctx, sub: _Sub) -> None:
    if not _begin(ctx, sub, "config"):
        return
    cv = ctx.view("config")
    ctx.step("config_view", f"{len(cv['env_vars'])} env vars")
    files = {i["file"]: i["score"] for i in sub.items}
    stems = [tn.en_stem(tn.fold_tr(t)) for t in sub.words if len(t) >= 3]

    def relevance(var: str, sites: list[dict]) -> tuple:
        """Question words in the variable's name (``MAX`` counts for "maximum"), then how
        relevant the files reading it are."""
        words = set(re.findall(r"[a-z0-9]+", var.lower()))
        named = sum(1 for s in stems if any(w.startswith(s) or (len(w) >= 3 and s.startswith(w)) for w in words))
        read_in = max((files.get(s["at"].rpartition(":")[0], 0.0) for s in sites), default=0.0)
        return named, read_in

    ranked = sorted(((k, v, relevance(k, v)) for k, v in cv["env_vars"].items()),
                    key=lambda x: (-x[2][0], -x[2][1], x[0]))
    hits = {k: v for k, v, (named, read_in) in ranked if named or read_in}
    for var, sites in list(hits.items())[:4]:
        if not ctx.budget.ok:
            ctx.rec.skipped["config"] += 1
            continue
        s0 = sites[0]
        path, _, ln = s0["at"].rpartition(":")
        ctx.rec.claim(f"`{s0['symbol']}` in {path} reads environment variable {var} ({s0['at']})", kind="config",
                      status="statically_verified",
                      evidence=[(_src_ev(ctx.repo, path, int(ln), None, ctx.commit, env=var), "supports")],
                      subjects=[path], spec={"env": var})
    if not hits:
        _unknown(ctx, sub, {"question": SUBQUESTIONS["config"], "why": "no env reads matched",
                            "next_step": "check settings objects / framework config manually; indirect reads "
                                         "are outside the config view's coverage"})


def _h_tests(ctx: _Ctx, sub: _Sub) -> None:
    g = ctx.g
    if not _begin(ctx, sub, "tests"):
        return
    tv = ctx.view("tests")
    ctx.step("tests_view", f"{tv['tests']} test functions")
    observe: list[tuple[dict, list[str], str | None]] = []  # one tracer run for all targets
    for t in _targets(ctx, sub):
        if am.is_test_file(g.file(t)):
            continue
        if not ctx.budget.ok:
            ctx.rec.skipped["tests"] += 1
            continue
        label, f = g.label(t), g.file(t)
        key = f"{label} ({f}:{g.line(t)})"
        tests = tv["covered"].get(key, [])
        test_nodes = [n for n in g.G.nodes if g.label(n) in tests and am.is_test_file(g.file(n))]
        it = {"id": t, "symbol": label, "file": f}
        if not tests:
            # Adding or changing a test file can falsify this: it watches the tests. Its only support
            # is a pointer to the tests view (a search result), so it stays an inference at best.
            ctx.rec.claim(f"No test statically reaches `{label}`", kind="tests", status="weak_inference",
                          evidence=[(_tests_view_pointer(ctx, tv, key), "supports")], subjects=[f"{f}::{label}"],
                          spec={"watch": "tests"},
                          uncertainties=["static reachability misses fixtures, mocks, parametrized indirection"])
            observe.append((it, [], None))
            continue
        evs = [(_src_ev(ctx.repo, g.file(n), g.line(n), None, ctx.commit, test=g.label(n)), "supports")
               for n in test_nodes[:3]]
        static = ctx.rec.claim(f"Test code statically reaches `{label}` from: {', '.join(tests[:4])}", kind="tests",
                               status="strong_inference", evidence=evs, subjects=[f"{f}::{label}"],
                               spec={"tests": [_pytest_id(g, n) for n in test_nodes], "target": t},
                               uncertainties=["static call reachability, not runtime coverage: a test may stop "
                                              "(raise, return, mock) before it runs this code"])
        if ctx.run_tests:
            _run_tests(ctx.store, ctx.repo, g, it, test_nodes, ctx.commit, ctx.budget, ctx.rec, ctx.step,
                       lambda u: _unknown(ctx, sub, u), ctx.cfg)
        observe.append((it, test_nodes, (static or {}).get("id")))
    if ctx.observe and observe:
        _observe(ctx, sub, observe)


def _tests_view_pointer(ctx: _Ctx, tv: dict, key: str) -> dict:
    """A search-result pointer to the tests view's answer for ``key`` (never verifying evidence)."""
    method = (tv.get("coverage") or {}).get("method") or "static reachability"
    body = f"{key}: not reached by any of {tv.get('tests')} test functions ({method})"
    return {"source_type": "search_result", "locator": f"tests view: {key}", "path": None,
            "commit_sha": ctx.commit, "content_hash": evmod.content_hash(body), "excerpt": body,
            "meta": {"view": "tests", "method": method, "tests": tv.get("tests")}}


MAX_OBSERVED_TESTS = 20


def _runtime():
    """The runtime tracer module (:mod:`verinoda.runtime.trace`), or None when it is not importable."""
    try:
        from verinoda.runtime import trace as rt  # optional
    except Exception:  # noqa: BLE001 - not installed / not importable
        return None
    return rt


def _runtime_observe():
    rt = _runtime()
    return getattr(rt, "observe", None) if rt is not None else None


def _short_test(test_id: str) -> str:
    return test_id.rpartition("::")[2]


def _observe_terms(sub: _Sub) -> list[str]:
    """Question words that may name tests (``select_tests`` matches them in test function names)."""
    return [w for w in sub.words if len(w) >= 4 and w.isidentifier()][:6]


def _observe(ctx: _Ctx, sub: _Sub, targets: list[tuple[dict, list[str], str | None]]) -> None:
    """Run the tests likely to reach the targets under the call tracer, once (docs/DESIGN.md D28).

    ``targets``: ``(item, statically reaching test nodes, static tests claim id)`` per symbol.

    Tests are chosen by :func:`verinoda.runtime.trace.select_tests` (static reachability, then
    test names carrying the target's or the question's words). The run is recorded; what it
    shows becomes evidence: per-test reach evidence for the tests claim (an observed reach set
    supersedes a static claim it contradicts) and observed call edges for relation claims
    (attached after all sub-questions ran, see :func:`_apply_observations`).
    """
    question = f"which tests reach {', '.join(it['symbol'] for it, _, _ in targets)} at runtime?"
    fn = _runtime_observe()
    if fn is None:
        _unknown(ctx, sub, {"question": question, "why": "runtime observation is not available in this installation",
                            "next_step": "run the tests with the runtime tracer (verinoda runtime extra), or use "
                                         "--run for a pass/fail check"})
        return
    rt = _runtime()
    static: dict[str, list[str]] = {it["id"]: list(dict.fromkeys(
        _pytest_id(ctx.g, n) for n in nodes if (ctx.g.file(n) or "").endswith(".py"))) for it, nodes, _ in targets}
    picked: list[str] = []
    if rt is not None and hasattr(rt, "select_tests"):
        try:
            picked = list(rt.select_tests(ctx.g, [it["id"] for it, _, _ in targets], terms=_observe_terms(sub),
                                          limit=MAX_OBSERVED_TESTS))
        except Exception:  # noqa: BLE001 - selection is a heuristic; the static set still stands
            picked = []
    ids = list(dict.fromkeys([x for v in static.values() for x in v] + picked))[:MAX_OBSERVED_TESTS]
    if not ids or ctx.budget.remaining_seconds() < 1 or not ctx.budget.work_ok():
        _unknown(ctx, sub, {"question": question,
                            "why": "no Python test was selected for it (static reachability or test names)"
                                   if not ids else "no time or tool-call budget left for a test run",
                            "next_step": "`verinoda observe --for <symbol>` or observe the whole suite"})
        return
    ctx.budget.spend(1)
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    kw = {"timeout": min(float(ctx.cfg["experiments"]["default_timeout"]), ctx.budget.remaining_seconds()),
          "snapshot": ctx.snap}
    if "graph" in params:
        kw["graph"] = ctx.g
    if "targets" in params:
        kw["targets"] = [it["id"] for it, _, _ in targets]
    try:
        res = fn(ctx.store, ctx.repo, ids, **kw) or {}
    except Exception as exc:  # noqa: BLE001 - a failed observation is an unknown, not a crash
        why = f"runtime observation failed: {type(exc).__name__}: {exc}"[:300]
        _unknown(ctx, sub, {"question": question, "why": why, "next_step": "run the tests manually under the tracer"})
        return
    ctx.step("runtime_observe", f"run {res.get('run_id')}: {len(ids)} test(s), complete={res.get('complete')}")
    if res.get("error"):
        _unknown(ctx, sub, {"question": question, "why": str(res["error"])[:300],
                            "next_step": res.get("next_step") or "fix the test environment and observe again"})
        return
    ctx.observations.append(res)
    for it, _, static_cid in targets:
        _observed_reach(ctx, sub, it, res, static[it["id"]], static_cid)
    # results of other tracer versions may carry ready-made claims and unknowns
    for c in res.get("claims") or []:
        cid = c if isinstance(c, str) else (c or {}).get("id")
        if cid and ctx.store.claim(cid) and cid not in ctx.rec.ids:
            ctx.rec.ids.append(cid)
            ctx.rec._note(cid)
    for u in res.get("unknowns") or []:
        if isinstance(u, dict):
            _unknown(ctx, sub, u)


def _target_qual(ctx: _Ctx, res: dict, t: str) -> str:
    """The tracer's qualified name of node ``t`` (as the observed edges name it), else the syntax facts'."""
    for e in (res.get("edges") or []):
        callee = e.get("callee") or {}
        if callee.get("node") == t and callee.get("qual"):
            return callee["qual"]
    for ev in res.get("evidence") or []:
        meta = ev.get("meta") or {}
        if meta.get("callee_node") == t and meta.get("callee_qual"):
            return meta["callee_qual"]
    name = qp._bare(ctx.g.label(t))
    try:
        facts = anchors.facts_for(ctx.store, ctx.repo, ctx.g.file(t) or "")
    except Exception:  # noqa: BLE001
        facts = None
    for q, s in anchors.symbols_named(facts, name):
        if ctx.g.line(t) in (s["start"], s["def"]):
            return q.split("#")[0]
    return name


def _observed_reach(ctx: _Ctx, sub: _Sub, it: dict, res: dict, static_ids: list[str],
                    static_cid: str | None) -> None:
    """Which of the selected tests reached ``it`` in the run: one claim, per-test reach evidence."""
    rt = _runtime()
    run_id = res.get("run_id")
    if rt is None or not run_id or not hasattr(rt, "reach_evidence"):
        return
    g, t, label = ctx.g, it["id"], it["symbol"]
    path, qual = g.file(t) or "", _target_qual(ctx, res, t)
    # the tests static reachability named, and every test the run saw reaching it
    reached_ids = list((res.get("target_reach") or {}).get(t) or [])
    outcome: dict[str, tuple[str, dict]] = {}
    for tid in list(dict.fromkeys(static_ids + reached_ids))[:MAX_OBSERVED_TESTS]:
        try:
            ev = rt.reach_evidence(ctx.store, ctx.repo, run_id, tid, path, qual, hypothesis="reached")
        except Exception:  # noqa: BLE001
            ev = None
        if ev is not None:
            outcome[tid] = ((ev.get("meta") or {}).get("outcome") or "inconclusive", ev)
    reached = [tid for tid, (o, _) in outcome.items() if o == "pass"]
    missed = [tid for tid, (o, _) in outcome.items() if o == "fail"]
    unsure = [tid for tid, (o, _) in outcome.items() if o not in ("pass", "fail")]
    cl = ctx.rec.cl
    for tid in unsure[:3]:
        ev = outcome[tid][1]
        why = (ev.get("meta") or {}).get("inconclusive_reason") or "the run cannot tell"
        if static_cid and tid in static_ids:
            cl.attach(static_cid, ev, "qualifies", note=f"runtime run {run_id}: inconclusive")
        _unknown(ctx, sub, {"question": f"does {_short_test(tid)} reach {label} at runtime?", "why": why,
                            "next_step": "observe again with a larger --budget-seconds, or fix the test"})
    if not reached and not missed:
        return
    commit = (res.get("commit") or ctx.commit or "")[:10]
    text = (f"Observed in run {run_id} (commit {commit}): `{label}` was reached by "
            + (", ".join(_short_test(x) for x in reached[:6]) or "none of the tests run")
            + (f"; not by {', '.join(_short_test(x) for x in missed[:6])}" if missed else ""))
    evs: list[tuple] = [(outcome[x][1], "supports", "reach") for x in reached]
    for x in missed:
        try:
            neg = rt.reach_evidence(ctx.store, ctx.repo, run_id, x, path, qual, hypothesis="not_reached")
        except Exception:  # noqa: BLE001
            neg = None
        if neg is not None:
            evs.append((neg, "supports", "reach"))
    over = [x for x in missed if x in static_ids]
    # every test the claim names is in ``tests`` (its dependencies); ``reached`` / ``not_reached`` split them
    spec = {"experiment": res.get("experiment_id"), "run_id": run_id, "tests": reached + missed,
            "reached": reached, "not_reached": missed, "target": t, "observed": True}
    unc = ["run-scoped: what these tests executed at this commit, never what always happens"]
    if over and static_cid:
        unc.append(f"static reachability listed {', '.join(_short_test(x) for x in over)}, which the run shows "
                   "not reaching it")
    new = ctx.rec.claim(text, kind="test_run", status="experiment_verified", evidence=evs,
                        subjects=[f"{path}::{label}"], spec=spec, uncertainties=unc,
                        supersedes=static_cid if over else None)
    if new is None or not (over and static_cid):
        return
    # the static claim over-states: the run refutes the tests it listed that never reached the code
    for x in over:
        cl.attach(static_cid, outcome[x][1], "refutes", note=f"runtime run {run_id}: not reached")
    try:
        cl.set_status(static_cid, "contradicted", reason=f"observed reach set of run {run_id} excludes "
                      + ", ".join(_short_test(x) for x in over), actor="analysis", downgrade=True,
                      extra_fields={"superseded_by": new["id"]}, payload={"superseded_by": new["id"]})
    except Exception:  # noqa: BLE001 - the static claim keeps its status; the new claim stands on its own
        return
    ctx.replaced[static_cid] = new["id"]


def _observed_edge_matches(ctx: _Ctx, spec: dict, ev: dict) -> bool:
    """Does ``call_trace`` evidence show the relation claim's call (same call line, same callee)?"""
    meta = ev.get("meta") or {}
    if meta.get("kind") != "call_trace" or meta.get("outcome") != "pass":
        return False
    if (meta.get("flags") or {}).get("test_double") or meta.get("external_callee"):
        return False  # a test double never supports a production edge
    at = spec.get("at")
    if not at or f"{ev.get('path')}:{ev.get('line_start')}" != at:
        return False
    t = spec.get("target")
    if meta.get("callee_node"):
        return meta["callee_node"] == t
    return meta.get("callee_path") == ctx.g.file(t) and \
        entail._token(meta.get("callee_qual")) == entail._token(spec.get("target_label"))


def _apply_observations(ctx: _Ctx) -> None:
    """Observed calls support the relation claims whose call site and callee they show.

    Attaching changes nothing by itself; the claim is then re-assessed at ``experiment_verified``
    ("observed in run R at commit C"), which the evidence rules accept or refuse.
    """
    if not ctx.observations:
        return
    cl = ctx.rec.cl
    for cid in list(ctx.rec.ids):
        c = ctx.store.claim(cid)
        if not c or c.get("kind") != "relation" or c.get("superseded_by") or \
                c["status"] in ("contradicted", "stale"):
            continue
        spec = c.get("spec") or {}
        for res in ctx.observations:
            ev = next((e for e in res.get("evidence") or [] if _observed_edge_matches(ctx, spec, e)), None)
            if ev is None:
                continue
            cl.attach(cid, ev, "supports", note=f"call observed in runtime run {res.get('run_id')}")
            ctx.step("observed_edge", f"{spec.get('at')} (run {res.get('run_id')})")
            if c["status"] != "experiment_verified":
                try:
                    cl.set_status(cid, "experiment_verified", actor="analysis", downgrade=False,
                                  reason=f"call observed in runtime run {res.get('run_id')} at commit "
                                         f"{(res.get('commit') or '')[:10]}")
                except ClaimRuleError:
                    pass  # the rules refused it (recorded in the claim's history); the status stands
            break


def _decision_terms(ctx: _Ctx, sub: _Sub) -> list[str]:
    text = sub.sq.get("text_user_lang") or sub.sq.get("text") or ""
    cue_words = {"why", "neden", "nicin", "niye", "reason", "rationale", "decision", "decided", "gerekce", "karar"}
    words = [w for w in _content_words(text) if w not in cue_words and w not in qp._GENERIC_EN and len(w) >= 3]
    exp = []
    for w in words:
        for k, v in (sub.extra.get("expansions") or {}).items():
            if w in k.split():
                exp += v
    return list(dict.fromkeys(words + exp))


def _h_why(ctx: _Ctx, sub: _Sub) -> None:
    g, rec, repo = ctx.g, ctx.rec, ctx.repo
    if not _begin(ctx, sub, "why"):
        return
    hv = ctx.view("history")
    ctx.step("history_view", f"{len(hv['decisions'])} decision docs, git={hv['is_git']}")
    names = {i["symbol"].strip(".()") for i in sub.items} | {Path(i["file"]).name for i in sub.items}
    files = {i["file"] for i in sub.items}
    terms = _decision_terms(ctx, sub)
    found = False
    for dec in hv["decisions"]:
        text = "\n".join(am._read(repo, dec["doc"]))
        folded = tn.fold_tr(text).lower()
        sym_hit = sorted(set(dec["mentions_symbols"]) & names)
        file_hit = sorted(set(dec["mentions_files"]) & files)
        name_words = {tn.fold_tr(w).lower() for s in sym_hit for w in tn.split_identifier(s)} | \
                     {tn.fold_tr(s).lower() for s in sym_hit} | \
                     {tn.fold_tr(Path(f).stem).lower() for f in file_hit} | \
                     {tn.fold_tr(n).lower() for n in names}
        topic = [t for t in terms if t not in name_words]
        topic_hits = [t for t in topic if _has_term(folded, t)]
        explains = bool(topic_hits) and len(topic_hits) >= max(1, (len(topic) + 1) // 2)
        if not (sym_hit or file_hit or (explains and len(topic_hits) >= 2)):
            continue
        if not ctx.budget.ok:
            rec.skipped["why"] += 1
            continue
        body = next((ln for ln in text.splitlines()[1:] if ln.strip() and not ln.lower().startswith("status")), "")
        ev = evmod.source_evidence(repo, dec["doc"], 1, min(len(text.splitlines()), 12), commit=ctx.commit,
                                   source_type="design_doc")
        if explains:
            c = rec.claim(f"Decision record {dec['doc']} (status: {dec['status']}) explains it: {body.strip()[:160]}",
                          kind="decision", status="primary_source_verified", evidence=[(ev, "supports")],
                          subjects=[dec["doc"]], spec={"topic_terms": topic_hits})
        else:
            what = ", ".join(f"`{s}`" for s in sym_hit[:3]) or ", ".join(file_hit[:3])
            c = rec.claim(f"Decision record {dec['doc']} (status: {dec['status']}) mentions {what}: "
                          f"{body.strip()[:140]}", kind="decision", status="strong_inference",
                          evidence=[(ev, "supports")], subjects=[dec["doc"]],
                          uncertainties=[f"matched only because the document names {what}; it may not state the "
                                         "reason that was asked about"])
        if c is not None:
            found = True
    for it in sub.prod[:1]:
        sp = g.span(it["id"]) if it["id"] in g.G else None
        if not sp or not hv["is_git"] or not ctx.budget.ok:
            continue
        a, b = sp
        log = git(repo, "log", "-n3", f"-L{a},{b}:{it['file']}", "--no-patch", "--format=%H%x1f%aI%x1f%s")
        ctx.step("git_log_L", f"{it['file']}:{a}-{b}")
        for line in (log or "").splitlines():
            if "\x1f" not in line:
                continue
            sha, date, subj = line.split("\x1f", 2)
            ev = {"source_type": "git_history", "locator": f"commit {sha}", "commit_sha": sha,
                  "content_hash": evmod.content_hash(subj), "excerpt": subj, "meta": {"date": date}}
            if rec.claim(f"`{it['symbol']}` lines {a}-{b} were changed in {sha[:10]} ({date[:10]}): {subj}",
                         kind="history", status="primary_source_verified", evidence=[(ev, "supports")],
                         subjects=[it["file"]]) is not None:
                found = True
    if not found and not rec.skipped["why"]:
        _unknown(ctx, sub, {"question": sub.sq.get("text") or SUBQUESTIONS["why"],
                            "why": "no decision record or commit message explains it",
                            "next_step": "search issue tracker/PR discussion; or `verinoda research <reference>` "
                                         "if the design follows an external reference"})
    elif found:
        _unknown(ctx, sub, {"question": "is the recorded rationale still the actual reason?",
                            "why": "documents state intent, not current behaviour",
                            "next_step": "compare with code via `verinoda challenge <claim>`"})


def _h_impact(ctx: _Ctx, sub: _Sub) -> None:
    g = ctx.g
    targets_n = _targets(ctx, sub)
    if not targets_n or not _begin(ctx, sub, "impact"):
        return
    targets = sorted({g.file(n) for n in targets_n if g.file(n)})
    iv = am.impact(g, targets)
    ctx.step("impact_view", f"{len(iv['affected_files'])} files")
    if iv["affected_files"]:
        dep_edges = [(u, v, d) for u, v, d in g.edges({"calls", "imports_from", "uses"})
                     if g.file(v) in targets and g.file(u) in iv["affected_files"]][:3]
        evs = [(evmod.graph_edge_evidence({"source": u, "target": v, **d}, graph_path=str(g.path), commit=ctx.commit),
                "supports") for u, v, d in dep_edges]
        ctx.rec.claim(f"Changing {', '.join(targets)} possibly affects {', '.join(list(iv['affected_files'])[:8])}; "
                      f"tests to run: {', '.join(iv['tests_to_run']) or 'none found'}",
                      kind="impact", status="strong_inference", evidence=evs, subjects=targets,
                      uncertainties=iv["coverage"]["limits"])


_NAME = r"`?([\w.]+?)(?:\(\))?`?"
_BEFORE = re.compile(rf"^\s*{_NAME}\s+before\s+{_NAME}\s+in\s+`?([\w./:]+?)(?:\(\))?`?\s*$", re.I)
_REGEX_PROP = re.compile(r"^\s*regex:(.+?)(?:\s+in:(\S+))?\s*$")


def _h_behaviour(ctx: _Ctx, sub: _Sub) -> None:
    prop = ((sub.sq.get("done_when") or {}).get("proposition") or "").strip()
    if not prop:
        sub.flags["not_supported"] = "no checkable proposition"
        _unknown(ctx, sub, {"question": sub.sq.get("text") or SUBQUESTIONS["behaviour"],
                            "why": "a behaviour question needs a checkable proposition; the claims above only say "
                                   "where the code is",
                            "next_step": "write done_when.proposition as 'A before B in F' or "
                                         "'regex:<pattern> in:<glob>' in the plan and re-run"})
        return
    if not _begin(ctx, sub, "behaviour"):
        return
    m = _REGEX_PROP.match(prop)
    if m:
        from verinoda import feedback

        res = feedback._eval_proposition(ctx.store, ctx.repo, m.group(1), m.group(2), commit=ctx.commit,
                                         external=False)
        ctx.step("proposition", f"regex {m.group(1)!r} in {m.group(2) or '*'}: {res.get('hit_count')} hit(s)")
        if res.get("error"):
            sub.flags["not_supported"] = res["error"]
            _unknown(ctx, sub, {"question": prop, "why": res["error"], "next_step": "fix the pattern"})
            return
        sub.extra["proposition"] = {"holds": res["holds"], "hits": [h["at"] for h in res["hits"]]}
        if res["holds"]:
            for h in res["hits"][:3]:  # one entailable claim per matching line
                ctx.rec.claim(f"{h['at']} contains `{m.group(1)}`", kind="behaviour", status="statically_verified",
                              evidence=[(h["evidence_id"], "supports")], subjects=[h["path"]],
                              spec={"proposition": prop, "pattern": m.group(1)})
        else:
            ctx.rec.claim(f"Pattern /{m.group(1)}/ does not occur in {m.group(2) or 'the repository'} "
                          f"({res['files_scanned']} files scanned)", kind="behaviour", status="weak_inference",
                          evidence=[], subjects=[], spec={"proposition": prop, "watch": "all"},
                          uncertainties=["absence found by a text scan; generated or excluded files are not seen"])
        return
    m = _BEFORE.match(prop)
    if not m:
        sub.flags["not_supported"] = "proposition form not recognised"
        _unknown(ctx, sub, {"question": prop, "why": "the proposition is neither 'A before B in F' nor 'regex:...'",
                            "next_step": "rewrite done_when.proposition in one of the two supported forms"})
        return
    _call_order(ctx, sub, m.group(1), m.group(2), m.group(3), prop)


def _call_order(ctx: _Ctx, sub: _Sub, a: str, b: str, where: str, prop: str) -> None:
    """``A before B in F``: first call lines of A and B inside F's definition (Python AST)."""
    import ast

    g = ctx.g
    nid, _ = g.resolve(where)
    if not nid or not (g.file(nid) or "").endswith(".py") or not g.span(nid):
        sub.flags["not_supported"] = f"{where} is not a Python function in the graph"
        _unknown(ctx, sub, {"question": prop, "why": f"cannot find a Python definition for '{where}'",
                            "next_step": "name the function as written in the code"})
        return
    f, (s, e) = g.file(nid), g.span(nid)
    try:
        tree = ast.parse((ctx.repo / f).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError) as exc:
        sub.flags["not_supported"] = str(exc)
        return
    first: dict[str, int] = {}
    branchy = False
    for node in ast.walk(tree):
        if not (s <= getattr(node, "lineno", 0) <= e):
            continue
        if isinstance(node, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.Match if hasattr(ast, "Match")
                             else ast.If)):
            branchy = True
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
            for want in (a, b):
                if name == want.rpartition(".")[2] and (want not in first or node.lineno < first[want]):
                    first[want] = node.lineno
    if a not in first or b not in first:
        missing = [x for x in (a, b) if x not in first]
        sub.extra["proposition"] = {"holds": False, "missing": missing}
        # a negative: stated as checked, but calls through other names or helpers are not followed
        ctx.rec.claim(f"`{qp._bare(g.label(nid))}` does not call {', '.join(f'`{x}`' for x in missing)} "
                      f"({f}:{s}-{e})", kind="behaviour", status="strong_inference",
                      evidence=[(_src_ev(ctx.repo, f, s, e, ctx.commit), "supports")],
                      subjects=[f"{f}::{g.label(nid)}"], spec={"proposition": prop},
                      uncertainties=["calls through other names or helpers are not followed"])
        return
    holds = first[a] < first[b]
    x, y = (a, b) if holds else (b, a)
    sub.extra["proposition"] = {"holds": holds, "lines": {a: first[a], b: first[b]}}
    unc = ["branches or loops in the function may change the order at runtime"] if branchy else []
    # the order is carried by the line numbers, which the evidence entails
    ctx.rec.claim(f"`{qp._bare(g.label(nid))}` calls `{x}` at {f}:{first[x]} and `{y}` at {f}:{first[y]}",
                  kind="behaviour", status="statically_verified",
                  # one citation covering both call sites, so a single evidence states the whole claim
                  evidence=[(_src_ev(ctx.repo, f, first[x], first[y], ctx.commit, check="call_order"), "supports")],
                  subjects=[f"{f}::{g.label(nid)}"], spec={"proposition": prop, "holds": holds}, uncertainties=unc)


_READABLE = ("pinned", "pinned_floating", "partially_resolved")


def _resolve_references(ctx: _Ctx, text: str) -> tuple[dict | None, str | None]:
    """``(compact resolution, error)`` of the references in ``text``, offline (docs/DESIGN.md D16)."""
    try:
        from verinoda import references  # optional
    except Exception:  # noqa: BLE001
        return None, None
    resolve, compact = getattr(references, "resolve", None), getattr(references, "compact", None)
    if resolve is None or compact is None:
        return None, None
    try:
        # analyze never goes to the network: "the version we use" comes from the local project
        res = resolve(ctx.store, ctx.repo, text, network="off", local_intent=True)
    except Exception as exc:  # noqa: BLE001 - never let the resolver break an analysis
        return None, f"{type(exc).__name__}: {exc}"[:200]
    return compact(res), None


def _h_compare(ctx: _Ctx, sub: _Sub) -> None:
    refs = {r["id"]: r for r in ctx.plan.get("references") or []}
    mine = [refs[r] for r in sub.sq.get("references") or [] if r in refs]
    checks = {rc["reference"]: rc for rc in ctx.check.get("references") or []}
    sub.flags["not_supported"] = "reference comparison runs through `verinoda research`/`compare`"
    question = sub.sq.get("text") or SUBQUESTIONS["compare_reference"]
    # the user's own words carry the versions exactly as written; the plan's reference texts are fragments
    text = sub.sq.get("text_user_lang") or sub.sq.get("text") or ctx.plan.get("user_message") or ""
    extra = [r.get("text") for r in mine if r.get("text") and r["text"] not in text]
    comp, error = _resolve_references(ctx, " ".join([text, *extra]).strip())
    ctx.step("reference_resolve", (f"{comp.get('id')}: {comp.get('status')}" if comp else f"unavailable {error or ''}")
             .strip())
    for r in mine:
        sub.extra.setdefault("references", []).append({"reference": r.get("id"), "check": checks.get(r["id"], {})})
    if comp is not None:
        sub.extra.setdefault("references", []).append({"resolution": comp})
    lines = (comp or {}).get("references") or []
    if lines:
        rid = comp.get("id")
        for ln in lines[:4]:
            what = f"{ln.get('what') or ln['id']}" + (f" @ {ln['pin']} (basis: {ln.get('basis')})" if ln.get("pin")
                                                       else "")
            if ln.get("status") in _READABLE:
                why = (f"{what} is resolved but not read: analyze stays offline"
                       + (f"; mismatches: {'; '.join(ln['mismatches'][:2])}" if ln.get("mismatches") else ""))
                nxt = f"`verinoda research --resolution {rid} --reference-id {ln['id']}` then `verinoda compare`"
            else:
                un = (ln.get("unresolved") or [{}])[0]
                why = f"{what} is {ln.get('status')}" + (f": {un.get('why')}" if un.get("why") else "")
                nxt = un.get("next_step") or "answer the resolver's questions, then `verinoda resolve` again"
            _unknown(ctx, sub, {"question": question, "why": why, "next_step": nxt})
        for q in (comp.get("questions_for_user") or [])[:2]:
            _unknown(ctx, sub, {"question": q.get("question") or "which version was meant?",
                                "why": f"reference {q.get('reference')} is ambiguous",
                                "next_step": "ask the user" + (f": {' | '.join(q['options'][:4])}"
                                                               if q.get("options") else "")})
        return
    for r in mine or [None]:
        rtext = (r or {}).get("text") or text
        spec = ((r or {}).get("version") or {}).get("spec")
        loc = (r or {}).get("locator") or rtext
        _unknown(ctx, sub, {"question": question,
                            "why": f"the reference '{rtext}' must be pinned and read before comparing"
                                   + (f" (version {spec})" if spec else " (no version given)")
                                   + (f"; the resolver failed: {error}" if error else ""),
                            "next_step": f"`verinoda resolve \"{rtext}\"`, then `verinoda research {loc}`"
                                         + (f" at {spec}" if spec else "") + " and `verinoda compare`"})


def _h_unsupported(ctx: _Ctx, sub: _Sub) -> None:
    sub.flags["not_supported"] = f"no dedicated handler for '{sub.sq['intent']}'"
    _unknown(ctx, sub, {"question": sub.sq.get("text") or sub.sq["intent"],
                        "why": f"'{sub.sq['intent']}' questions have no dedicated handler; the claims only say "
                               "where the relevant code is",
                        "next_step": "`verinoda map` for architecture views, or ask about a specific function"})


HANDLERS = {
    "locate": None, "define": None, "flow": _h_flow, "dataflow": _h_dataflow, "callers": _h_callers,
    "config": _h_config, "tests": _h_tests, "why": _h_why, "history": _h_why, "impact": _h_impact,
    "behaviour": _h_behaviour, "compare_reference": _h_compare, "performance": _h_unsupported,
    "architecture": _h_unsupported, "usage": _h_unsupported,
}
SECONDARY_OK = {"flow", "dataflow", "config", "tests", "why", "impact", "callers"}


# -- one sub-question -------------------------------------------------------------------------------

def _filter_scope(items: list[dict], constraints: dict) -> list[dict]:
    scope = constraints.get("scope_paths") or []
    excl = constraints.get("exclude_paths") or []

    def ok(f: str) -> bool:
        if scope and not any(f.startswith(s.rstrip("/") + "/") or f == s or fnmatch.fnmatch(f, s) for s in scope):
            return False
        return not any(f.startswith(s.rstrip("/") + "/") or f == s or fnmatch.fnmatch(f, s) for s in excl)

    return [i for i in items if ok(i.get("file") or "")]


def _run_subquestion(ctx: _Ctx, sq: dict, share: int | None) -> dict:
    g = ctx.g
    links_by_id = {lk["mention"]: lk for lk in ctx.check.get("links") or []}
    mentions = {m["id"]: m for m in ctx.plan.get("mentions") or []}
    links = [links_by_id[m] for m in sq.get("mentions") or [] if m in links_by_id]
    extra_unc = []
    blocked = [c for c in ctx.check.get("clarifications") or [] if sq["id"] in c.get("blocks", [])]
    for c in blocked:
        extra_unc.append(f"open clarification {c['id']}: {c['question_en']} - no single reading was assumed")
    for lk in links:
        if lk["status"] == "weak" and mentions.get(lk["mention"], {}).get("required", True) \
                and not qp._is_concept(mentions.get(lk["mention"], {})):
            extra_unc.append(lk.get("uncertainty") or f"'{lk['text']}' is only weakly linked")
    # A version the user named (or chose when asked) is never silently replaced by the working tree.
    refs = {r["id"]: r for r in ctx.plan.get("references") or []}
    versioned = [refs[r] for r in sq.get("references") or []
                 if r in refs and (refs[r].get("version") or {}).get("spec")]
    chosen = [a["choice"] for a in ctx.plan.get("answers") or [] if a.get("clarification_id") == "c-version"
              or any(a.get("clarification_id") == f"c-{r}" for r in sq.get("references") or [])]
    pinned_elsewhere = [f"{r.get('text')} ({r['version']['spec']})" for r in versioned] + chosen
    if pinned_elsewhere and sq["intent"] != "compare_reference":
        extra_unc.append(f"the question names {', '.join(pinned_elsewhere)}; these claims describe the working tree "
                         f"at {(ctx.commit or 'uncommitted')[:10]}")
    plan_ref = {"plan_id": ctx.plan_id, "sub_question": sq["id"], "mentions": sq.get("mentions") or []}
    ctx.rec.begin(sq["id"], share, plan_ref, extra_unc)
    subject_nodes = {}
    if sq.get("subject_from"):
        subject_nodes = {n: f"subject from {sq['subject_from']} ({g.label(n)})"
                         for n in list(ctx.located.get(sq["subject_from"], {}))[:3] if n in g.G}
    sub = _Sub(sq=sq, links=links, items=[], prod=[], seeds={}, words=[], subject_nodes=subject_nodes)
    out = {"id": sq["id"], "intent": sq["intent"], "text": sq.get("text"),
           **({"text_user_lang": sq["text_user_lang"]} if sq.get("text_user_lang")
              and sq.get("text_user_lang") != sq.get("text") else {}),
           **({"secondary_intents": sq["secondary_intents"]} if sq.get("secondary_intents") else {}),
           "done_when": sq.get("done_when"), "links_used": [lk["mention"] for lk in links
                                                            if lk["status"] in ("linked", "weak")]}
    # An unpinned version: the working tree is not the code the user asked about, so nothing is
    # claimed from it. A dangling "bunu"/"it" with nothing to point at blocks the same way.
    version_blocks = [c for c in blocked if c["kind"] == "version"]
    deixis_blocks = [c for c in blocked if c["kind"] == "deixis" and len(c["options"]) <= 1
                     and not links and not subject_nodes]
    if version_blocks or deixis_blocks:
        c = (version_blocks or deixis_blocks)[0]
        sub.flags["blocked"] = c["id"]
        why = (f"the question is about a version that is not pinned ({c['question_en']}); the working tree "
               "may differ from it" if c["kind"] == "version" else c["question_en"])
        _unknown(ctx, sub, {"question": sq.get("text") or "", "why": why,
                            "next_step": "answer the clarification (a tag, commit or date) and re-run with the "
                                         "answer recorded in the plan"
                                         + (", or `verinoda resolve` the reference" if c["kind"] == "version"
                                            else "")})
        return _finish_sub(ctx, sub, out, "none")
    required_unlinked = [lk for lk in links if lk["status"] in ("unlinked", "not_found")
                         and mentions.get(lk["mention"], {}).get("required", True)]
    usable = [lk for lk in links if lk["status"] in ("linked", "weak", "ambiguous")]
    for u in ctx.check.get("unknowns") or []:  # a name that does not exist first (plan check orders them)
        if u.get("about") in {lk["mention"] for lk in required_unlinked}:
            _unknown(ctx, sub, {k: v for k, v in u.items() if k != "about"})
    # a name written as code that the repository does not have: whatever else is found, the
    # sub-question as asked is not answered
    not_found = [lk["mention"] for lk in required_unlinked if lk["status"] == "not_found"]
    if not_found:
        sub.flags["not_found"] = not_found
    if pinned_elsewhere and sq["intent"] != "compare_reference":
        _unknown(ctx, sub, {"question": sq.get("text") or "",
                            "why": f"the question names {', '.join(pinned_elsewhere)}; only the working tree was "
                                   "analysed",
                            "next_step": "read that version with `verinoda research <repository> --ref <version>` "
                                         "(or `verinoda resolve` the reference first)"})
    if required_unlinked and not usable and not subject_nodes:
        sub.flags["unlinked"] = [lk["mention"] for lk in required_unlinked]
        return _finish_sub(ctx, sub, out, "none")
    inputs = qp.retrieval_inputs(ctx.plan, ctx.check, sq, ctx.lex, subject_nodes=subject_nodes)
    sub.seeds = inputs["seeds"]
    sub.extra["expansions"] = inputs["expansions"]
    include_tests = (ctx.plan.get("constraints") or {}).get("include_tests", True)
    r = _retrieve(ctx, inputs, include_tests)
    raw = _filter_scope(r.get("items") or [], ctx.plan.get("constraints") or {})
    ctx.step("retrieve", f"{sq['id']}: {len(raw)} items" + (f", seeds={len(inputs['seeds'])}" if inputs["seeds"]
                                                             else "")
             + (f", expansions={sorted(inputs['expansions'])}" if inputs["expansions"] else ""))
    groups = _anchor_groups(ctx, sub, inputs["expansions"])
    content, missing = _ungrounded(ctx, sq, links, inputs["expansions"])
    anchored = set(inputs["seeds"]) | set(subject_nodes)
    for lk in links:
        anchored |= set(qp.mention_nodes(lk))
    sub.extra["anchored"], sub.extra["groups"] = anchored, groups
    rel = []
    for it in raw:
        score = _relevance(it, groups)
        if it["id"] in anchored or score >= RELEVANCE_MIN:
            rel.append(it)
    sub.items = rel
    sub.prod = [i for i in rel if not am.is_test_file(i["file"])]
    sub.words = list(dict.fromkeys(content + [t for v in inputs["expansions"].values() for t in v]
                                   + [p for n in subject_nodes for p in tn.split_identifier(qp._bare(g.label(n)))]))
    out["retrieval"] = {"items": len(raw), "relevant": len(rel)}
    if not raw:
        _unknown(ctx, sub, {"question": sq.get("text") or "", "why": "no graph node or text matched the question terms",
                            "next_step": "rephrase with a symbol/file name (identifiers as written in the code), "
                                         "or run `verinoda map` to browse"})
    if len(missing) >= 2 and len(missing) > UNGROUNDED_MAJORITY * max(1, len(content)):
        sub.flags["ungrounded_words"] = missing
        _unknown(ctx, sub, {"question": sq.get("text") or "",
                            "why": f"the question's words {', '.join(repr(w) for w in missing[:5])} occur nowhere "
                                   "in this repository (code, comments or docs)",
                            "next_step": "use the names the code uses (see `verinoda map`), or research the "
                                         "external system the question is about"})
    if raw and not rel:
        sub.flags["irrelevant"] = True
        _unknown(ctx, sub, {"question": sq.get("text") or "",
                            "why": f"none of the {len(raw)} retrieved items carries the question's grounded words "
                                   f"(relevance < {RELEVANCE_MIN})",
                            "next_step": "rephrase with a symbol/file name, or check the plan's mention links"})
    handler_names = []
    if rel or subject_nodes:
        _context_claims(ctx, sub, raw)
        for intent in [sq["intent"]] + [i for i in sq.get("secondary_intents") or [] if i in SECONDARY_OK]:
            h = HANDLERS.get(intent)
            if h is None or h.__name__ in handler_names:
                continue
            if intent != sq["intent"] and intent == "dataflow" and "_h_flow" in handler_names:
                continue
            handler_names.append(h.__name__)
            h(ctx, sub)
    elif sq["intent"] in ("behaviour", "compare_reference", "performance", "architecture", "usage"):
        HANDLERS[sq["intent"]](ctx, sub)
    if ctx.run_tests and sq["intent"] != "tests" and "_h_tests" not in handler_names and (rel or subject_nodes):
        _h_tests(ctx, sub)
    return _finish_sub(ctx, sub, out, "+".join(h[3:] for h in handler_names) or "location")


def _finish_sub(ctx: _Ctx, sub: _Sub, out: dict, handler: str) -> dict:
    for (sq_id, section), n in ctx.rec.sq_skipped.items():
        if sq_id == sub.sq["id"] and n and (sq_id, section) in ctx.started:
            _unknown(ctx, sub, {"question": SUBQUESTIONS.get(section, section),
                                "why": f"{ctx.budget.exhausted_reason or 'budget share for this sub-question spent'}; "
                                       f"{n} further claim(s) for it were not produced",
                                "next_step": NEXT_BUDGET})
    out["handler"] = handler
    out["claim_ids"] = list(ctx.rec.by_sq.get(sub.sq["id"], []))
    out["unknowns"] = sub.unknowns
    out["_flags"] = sub.flags
    # made for this sub-question only as context (definitions of search hits, links among them), not by
    # its handler: a claim is shared by text, so a caller a handler found stays an answer
    out["_context"] = [cid for cid in ctx.rec.by_sq.get(sub.sq["id"], [])
                       if {sec for sq, sec in ctx.rec.made_in.get(cid, ()) if sq == sub.sq["id"]} <= CONTEXT_SECTIONS]
    if sub.extra.get("proposition") is not None:
        out["proposition"] = sub.extra["proposition"]
    if sub.extra.get("references"):
        out["references"] = sub.extra["references"]
    return out


# -- verdicts -----------------------------------------------------------------------------------------

DONE_CLAIM_KINDS = {
    "location_verified": {"location"},
    "path_found": {"flow"},
    "set_enumerated": {"tests", "impact", "relation", "test_run"},
    "decision_found": {"decision", "history"},
    "proposition_checked": {"behaviour"},
    "reference_pinned": {"reference"},
    "comparison_done": {"comparison"},
}
CLAIM_EXISTS_KINDS = {
    "dataflow": {"flow"}, "config": {"config"}, "flow": {"flow"}, "tests": {"tests", "test_run"},
    "callers": {"relation"}, "impact": {"impact"}, "why": {"decision", "history"}, "history": {"history"},
    "behaviour": {"behaviour"},
}
_RANK = {s: i for i, s in enumerate(ORDER)}


def _verdict_kinds(sq: dict) -> tuple[str, str, set]:
    """``(done_when kind, min_status, claim kinds that can meet it)`` for a sub-question."""
    dw = sq.get("done_when") or {}
    kind = dw.get("kind") or qp.DEFAULT_DONE.get(sq.get("intent"), ("claim_exists",))[0]
    min_status = dw.get("min_status") or qp.DEFAULT_DONE.get(sq.get("intent"), (None, MIN_STATUS_DEFAULT))[1]
    kinds = DONE_CLAIM_KINDS.get(kind) or set()
    if kind == "claim_exists":
        kinds = CLAIM_EXISTS_KINDS.get(sq.get("intent"), {"location", "flow", "config", "relation"})
    if kind == "set_enumerated" and sq.get("intent") in CLAIM_EXISTS_KINDS:
        kinds = CLAIM_EXISTS_KINDS[sq["intent"]]
    return kind, min_status, kinds


def answer_claims(sq: dict, claims: list[dict], flags: dict | None = None) -> list[str]:
    """The claims that answer a sub-question, strongest first: of a kind its ``done_when`` asks for,
    about its subject (not ``off_subject`` context), not stale, contradicted or unknown."""
    _kind, _min, kinds = _verdict_kinds(sq)
    off = set((flags or {}).get("off_subject") or [])
    ans = [c for c in claims if c.get("kind") in kinds and c["id"] not in off
           and c.get("status") in _RANK and c["status"] not in ("unknown", "stale", "contradicted")]
    def in_tests(c: dict) -> bool:  # the product's own code first (a caller in the API before one in a test)
        subs = c.get("subjects") or []
        if isinstance(subs, str):
            try:
                subs = json.loads(subs)
            except ValueError:
                subs = [subs]
        first = next((str(s).split("::", 1)[0] for s in subs if s), "")  # a relation's first subject: the caller
        return bool(first) and am.is_test_file(first)

    return [c["id"] for c in sorted(ans, key=lambda c: (in_tests(c), _RANK[c["status"]]))]


def judge(sq: dict, claims: list[dict], flags: dict | None = None) -> str:
    """Verdict of one sub-question from the current state of the claims it produced.

    ``met``: the strongest relevant claim is verified and at least
    ``done_when.min_status``; ``met_with_inference``: relevant claims exist but
    only at a weaker level, or only about code ranked near the subject rather than
    the subject itself (``flags["off_subject"]``: context claims for other symbols);
    ``unmet``: none (stale, contradicted and unknown claims never count), or the sub-question names
    code that does not exist here (``flags["not_found"]``: claims about other names do not answer it);
    ``not_supported`` / ``blocked_by_clarification`` come from the handler.
    """
    flags = flags or {}
    if flags.get("blocked"):
        return "blocked_by_clarification"
    if flags.get("not_found"):
        return "unmet"
    kind, min_status, kinds = _verdict_kinds(sq)
    live = [c for c in claims if c.get("kind") in kinds and c.get("status") in _RANK and c["status"] != "unknown"]
    off = set(flags.get("off_subject") or [])
    about = [c for c in live if c["id"] not in off]  # context claims about other code than the question's
    if live and not about:
        return "met_with_inference"  # evidence exists, but none of it is shown to be about what was asked
    live = about
    if not live:
        if flags.get("not_supported"):
            return "not_supported"
        if kind == "set_enumerated" and flags.get("empty_sets") and not flags.get("unlinked"):
            return "met_with_inference"
        return "unmet"
    best = min(live, key=lambda c: _RANK[c["status"]])["status"]
    if best in VERIFIED and _RANK[best] <= _RANK.get(min_status, _RANK[MIN_STATUS_DEFAULT]):
        return "met"
    return "met_with_inference"


def _claim_rows(store: Store, ids: list[str]) -> list[dict]:
    rows = []
    for cid in ids:
        c = store.claim(cid)
        seen = set()
        while c is not None and c.get("superseded_by") and c["id"] not in seen:
            seen.add(c["id"])
            c = store.claim(c["superseded_by"])
        if c is not None:
            rows.append(c)
    return rows


# -- critique: counter-probes against "tests reach it" ---------------------------------------------------

def _challenge_kwargs(critique, g, current_files) -> dict:
    kw = {"graph": g}
    try:
        params = inspect.signature(critique.challenge).parameters
    except (TypeError, ValueError):
        params = {}
    if "current_files" in params and current_files is not None:
        kw["current_files"] = current_files
    return kw


def _refuted_tests(claim: dict, challenge: dict) -> list[str]:
    """Tests a critique probe says do not reach the target (refuting findings that name a test)."""
    tests = list((claim.get("spec") or {}).get("tests") or [])
    out = []
    for f in challenge.get("findings") or []:
        if f.get("result") not in ("fail", "refutes") or f.get("check") in ("support", "source_recheck", "staleness",
                                                                            "graph_only", "call_site"):
            continue
        named = f.get("test") or f.get("test_id")
        cands = [named] if named else [t for t in tests if t.rpartition("::")[2] and
                                       re.search(rf"\b{re.escape(t.rpartition('::')[2])}\b", f.get("detail") or "")]
        for t in cands:
            if t and t not in out:
                out.append(t)
    return out


def _correct_tests_claim(ctx: _Ctx, cid: str, refuted: list[str], challenge: dict) -> str | None:
    """Supersede a "tests statically reach X" claim that a counter-probe partly refutes."""
    def short(test_id: str) -> str:
        return test_id.rpartition("::")[2]

    cl = ctx.rec.cl
    c = cl.get(cid)
    gone = {short(r) for r in refuted}
    keep = [t for t in (c.get("spec") or {}).get("tests") or [] if t not in refuted and short(t) not in gone]
    target = re.search(r"reaches `([^`]+)`", c["text"])
    label = target.group(1) if target else "it"
    naming = [f for f in challenge.get("findings") or [] if f.get("result") in ("fail", "refutes")
              and (f.get("test") in refuted or any(g in (f.get("detail") or "") for g in gone))]
    details = "; ".join(f.get("detail") or "" for f in naming)[:300]
    evs = [(e["id"], "supports") for e in cl.evidence(cid) if e["relation"] == "supports"
           and (e.get("meta") or {}).get("test", "").strip(".()") in {short(k) for k in keep}]
    excluded = ", ".join(sorted(gone))
    if keep:
        text = (f"Test code statically reaches `{label}` from: {', '.join(short(k) + '()' for k in keep[:4])}"
                f" (counter-probe excluded {excluded})")
        status = "strong_inference"
    else:
        text = f"No test is shown to reach `{label}`: counter-probes refute {excluded}"
        status = "weak_inference"
    try:
        new = cl.supersede(cid, text, evidence=evs, status=status, reason=f"counter-probe: {details or 'refuted'}",
                           actor="analysis", snapshot=ctx.snap)
    except Exception:  # noqa: BLE001 - the original claim stays, with its critique result
        return None
    unc = [f"counter-probe: {details}" if details else "counter-probe refuted part of the static reachability"]
    try:
        ctx.store.update_claim(new["id"], {"uncertainties": unc + list(c.get("uncertainties") or [])})
    except Exception:  # noqa: BLE001
        pass
    return new["id"]


# -- the loop ---------------------------------------------------------------------------------------

def _lexicon(repo: Path, g: index.Graph, store: Store, snap: dict, step) -> object:
    """The repository lexicon, refreshed incrementally when the snapshot moved; graph-only fallback."""
    try:
        from verinoda import lexicon as lexmod
    except Exception:  # noqa: BLE001
        return None
    try:
        lex = lexmod.load(repo)
        if lex is None or lex.tree_hash != snap.get("tree_hash"):
            files = store.snapshot_files(snap["id"]) if snap.get("id") else None
            t0 = time.monotonic()
            stats = lexmod.build(repo, g, file_hashes=files or None, tree_hash=snap.get("tree_hash"))
            step("lexicon", f"built: {stats.get('files')} files, {stats.get('reparsed')} re-read, "
                            f"{stats.get('pairs')} pairs ({time.monotonic() - t0:.2f}s)")
            lex = lexmod.load(repo)
        return lex or lexmod.from_graph(g)
    except Exception as exc:  # noqa: BLE001 - no lexicon only means fewer candidates
        step("lexicon", f"unavailable ({type(exc).__name__}); seed glosses grounded on graph names only")
        try:
            return lexmod.from_graph(g)
        except Exception:  # noqa: BLE001
            return None


def analyze(store: Store, repo: Path, question: str, *, plan=None, budget: Budget | None = None,
            run_tests: bool = False, challenge: bool = True, max_claims: int = 12, graph=None,
            observe: bool = False) -> dict:
    """Answer ``question`` (or ``plan``: a dict, JSON text or a plan file) with claims and unknowns."""
    from verinoda import critique, workflow

    repo = Path(repo).resolve()
    cfg_all = load_config(repo)
    cfg = cfg_all["budget"]
    budget = budget or Budget(seconds=cfg["seconds"], tool_calls=cfg["tool_calls"],
                              context_tokens=cfg["context_tokens"])
    steps: list[dict] = []
    unknowns: list[dict] = []

    def step(name: str, detail: str = "") -> bool:
        rec_ = {"step": name, "detail": detail, "t": round(time.monotonic() - budget.started, 3)}
        steps.append(rec_)
        return budget.spend(1, _size(rec_))

    def unknown(u: dict) -> None:
        unknowns.append(u)
        budget.spend(0, _size(u))

    aid = new_id("ana")
    host_plan, plan_problems = (qp.parse(plan) if plan is not None else (None, []))
    if host_plan is not None and not (question or "").strip():
        question = str(host_plan.get("user_message") or "")
    # 0. make sure the index describes the working tree we are about to cite
    #    (the refresh is charged to the time budget like any other step)
    snap = store.latest_snapshot()
    state = current_state(repo, store=store)  # stat-cached hashes; passed once to critique
    extra_unc: list[str] = []
    if snap is None or snap["tree_hash"] != state["tree_hash"]:
        try:
            res = workflow.update(store, repo)
        except Exception as exc:  # noqa: BLE001 - a failed refresh is reported, not raised
            res = {"error": f"{type(exc).__name__}: {exc}"[:300], "snapshot": snap}
        if res.get("error") or not res.get("snapshot"):
            prev = res.get("snapshot") or snap
            err = res.get("error") or "the refresh produced no snapshot"
            extra_unc.append(f"index could not be refreshed ({err}); the graph describes an older "
                             f"tree (snapshot {(prev or {}).get('id')}), cited lines are re-read from the current one")
            unknown({"question": "does the index describe the current working tree?",
                     "why": f"index could not be refreshed: {err}",
                     "next_step": "run `verinoda scan --force`" + (f" ({res['hint']})" if res.get("hint") else "")})
            step("refresh_index", f"refused: {err}; continuing on snapshot {(prev or {}).get('id')}")
            snap = prev
        else:
            snap = res["snapshot"]
            step("refresh_index", f"working tree changed; re-indexed ({res.get('changed_count')} files), "
                                  f"{len(res.get('stale') or [])} claim(s) marked stale")
    if snap is None:  # nothing indexed to stand on
        result = {"analysis_id": aid, "question": question, "intents": intents_for(question), "status": "no_index",
                  "snapshot": None, "claims": [], "unknowns": unknowns, "critique": [], "steps": steps}
        result["usage"] = budget.usage()
        return result
    g = graph if graph is not None else index.load(repo)
    commit = snap["commit_sha"]
    lex = _lexicon(repo, g, store, snap, step)

    # 1-2. understand: the host's plan or one drafted by rules, then the deterministic check
    source = "host" if plan is not None else "fallback"
    if plan is None:
        the_plan = qp.draft(question, g, lex)
    else:
        the_plan = host_plan
    if the_plan is None:
        check_res = {"schema": qp.CHECK_SCHEMA_ID, "status": "invalid", "errors": plan_problems, "warnings": [],
                     "links": [], "references": [], "clarifications": [], "unknowns": []}
        stored_plan = {"unparsed": str(plan)[:20000]}
    else:
        check_res = qp.check(the_plan, g, repo, lex, source=source)
        stored_plan = the_plan
    plan_id = qp.store_plan(store, stored_plan, check_res, source, analysis_id=aid, snapshot_id=snap["id"])
    sqs = (the_plan or {}).get("sub_questions") or []
    step("plan", f"{source} plan {plan_id}: {check_res['status']}, {len(sqs)} sub-question(s)"
         + (f", {len(check_res.get('clarifications') or [])} clarification(s)" if check_res.get("clarifications")
            else ""))
    understood = (the_plan or {}).get("restated_goal_user_lang") or (the_plan or {}).get("restated_goal") or question
    intents = _legacy([sq.get("intent") for sq in sqs] + [i for sq in sqs for i in sq.get("secondary_intents") or []]) \
        or ["location"]
    base = {"analysis_id": aid, "question": question, "intents": intents, "plan_id": plan_id, "plan_source": source,
            "understood_as": understood,
            "snapshot": {"id": snap["id"], "commit": commit, "dirty": bool(snap["dirty"])}}
    on_ambiguity = (the_plan or {}).get("on_ambiguity") or ("ask" if source == "host" else "answer_all")
    if check_res["status"] == "invalid" or (check_res["status"] == "needs_clarification" and on_ambiguity == "ask"):
        status = "invalid_plan" if check_res["status"] == "invalid" else "needs_clarification"
        result = {**base, "status": status, "plan_check": qp.compact_check(check_res), "subquestions": [],
                  "claims": [], "unknowns": unknowns, "critique": [], "steps": steps}
        if status == "invalid_plan":
            result["errors"] = check_res["errors"]
        else:
            result["clarifications"] = check_res["clarifications"]
        result["usage"] = budget.usage()
        _store_analysis(store, aid, question, snap, budget, result, [], plan_id, status)
        return result
    rec = _Recorder(store, repo, snap, aid, budget, extra_unc)
    rec.precise_budget = _precise_budget(repo)
    budget.spend(0, _size(base) + 120)
    ctx = _Ctx(store=store, repo=repo, g=g, lex=lex, snap=snap, commit=commit, budget=budget, rec=rec, step=step,
               unknown=unknown, cfg=cfg_all, plan=the_plan, check=check_res, plan_id=plan_id, run_tests=run_tests,
               observe=observe)
    by_id = {sq["id"]: sq for sq in sqs}
    order = [i for i in check_res.get("topo_order") or [] if i in by_id] or list(by_id)
    subs: list[dict] = []
    for k, sq_id in enumerate(order):
        left = len(order) - k
        share = None if left == 1 else max(0, (budget.context_tokens * 4 - budget.chars) // left)
        subs.append(_run_subquestion(ctx, by_id[sq_id], share))
    # observed calls (runtime runs of this analysis) support the relation claims they show
    _apply_observations(ctx)

    # 8. critique (time and tool calls permitting; it only ever lowers confidence)
    challenged = []
    not_challenged: dict[str, str] = {}
    kw = _challenge_kwargs(critique, g, state.get("files"))
    replaced: dict[str, str] = dict(ctx.replaced)  # superseded during the analysis (observed reach sets)
    # what the handlers found is checked before what the search found around the question: with a
    # claim limit, the answer is what must not go unchallenged
    order = sorted(rec.ids, key=lambda c: all(sec in CONTEXT_SECTIONS for _sq, sec in rec.made_in.get(c, ())))
    for i, cid in enumerate(order):
        if cid in replaced:
            not_challenged[cid] = "superseded"
        elif not challenge:
            not_challenged[cid] = "disabled"
        elif i >= max_claims:
            not_challenged[cid] = "claim_limit"
        elif not budget.work_ok():
            not_challenged[cid] = "budget"
        else:
            budget.spend(1)
            try:
                ch = critique.challenge(store, repo, cid, **kw)
            except Exception as exc:  # noqa: BLE001 - an unchallenged claim says why
                not_challenged[cid] = f"critique_error: {type(exc).__name__}"
                continue
            challenged.append(ch)
            full = rec.cl.get(cid)
            if full.get("kind") == "tests":
                refuted = _refuted_tests(full, ch)
                if refuted:
                    new_id_ = _correct_tests_claim(ctx, cid, refuted, ch)
                    if new_id_:
                        replaced[cid] = new_id_
                        not_challenged[new_id_] = "correction_of_challenged_claim"
    for cid in rec.ids:
        if cid not in not_challenged and not any(c["claim"] == cid for c in challenged):
            not_challenged[cid] = "not_reached"
    out_ids = list(dict.fromkeys(replaced.get(cid, cid) for cid in rec.ids))
    out_claims = []
    for cid in out_ids:
        rc = rec.record(cid, not_challenged.get(cid))
        extra = _size(rc) - rec.charged.get(cid, 0)
        if extra > 0:  # status/confidence/flags changed after it was charged
            budget.chars += extra
        out_claims.append(rc)
    crit = [{"claim": c["claim"], "before": c["before"]["status"], "after": c["after"]["status"],
             "fails": [f["detail"] for f in c["findings"] if f["result"] == "fail"],
             "warns": [f["detail"] for f in c["findings"] if f["result"] == "warn"],
             **({"superseded_by": replaced[c["claim"]]} if c["claim"] in replaced else {})}
            for c in challenged if c["before"]["status"] != c["after"]["status"]
            or any(f["result"] != "pass" for f in c["findings"]) or c["claim"] in replaced]
    budget.chars += sum(_size(c) for c in crit)
    # 9. verdicts against done_when, on the claims as critique left them
    answering: list[str] = []
    for s in subs:
        s["claim_ids"] = list(dict.fromkeys(replaced.get(c, c) for c in s["claim_ids"]))
        flags = s.pop("_flags", {})
        if flags.get("off_subject"):  # critique may have superseded some: the new claim is context too
            flags["off_subject"] = list(dict.fromkeys(replaced.get(c, c) for c in flags["off_subject"]))
        context = {replaced.get(c, c) for c in s.pop("_context", [])}
        rows = _claim_rows(store, s["claim_ids"])
        s["status"] = judge(by_id[s["id"]], rows, flags)
        ans = answer_claims(by_id[s["id"]], rows, flags)
        # what the sub-question's own handler found (the write site, the callers) before definitions
        # of items the search ranked near the question
        # (a plain "where is X defined?" has no handler finding: its on-subject definitions answer)
        s["answer_claim_ids"] = [c for c in ans if c not in context] or [c for c in ans if c in context]
        answering += s["answer_claim_ids"]
        s["flags"] = {k: v for k, v in flags.items() if v}
        if not s["flags"]:
            del s["flags"]
    # what answers comes first (a reader, and a size cap that keeps the head, see it); the rest is context
    rank = {cid: i for i, cid in enumerate(dict.fromkeys(answering))}
    out_claims = sorted(out_claims, key=lambda c: rank.get(c["id"], len(rank)))
    result = {**base, "status": "answered", "plan_check": qp.compact_check(check_res), "subquestions": subs,
              "claims": out_claims, "unknowns": unknowns, "critique": crit, "steps": steps}
    if question.strip():
        result["passages"] = _passages(g, question)
        budget.chars += sum(len(ln) + 1 for ln in result["passages"])
    if extra_unc:
        result["index_refresh_error"] = extra_unc[0]
    result["usage"] = budget.usage()
    pb = rec.precise_budget
    if pb is not None and (pb.sites or pb.skipped):
        result["usage"]["precise"] = pb.as_dict()
    _store_analysis(store, aid, question, snap, budget, result, [c["id"] for c in out_claims], plan_id, "answered")
    return result


def _store_analysis(store: Store, aid: str, question: str, snap: dict, budget: Budget, result: dict,
                    claim_ids: list[str], plan_id: str, status: str) -> None:
    store.insert("analyses", {
        "id": aid, "question": question, "snapshot_id": snap["id"],
        "budget": {"seconds": budget.seconds, "tool_calls": budget.tool_calls, "context_tokens": budget.context_tokens},
        "usage": result["usage"],
        "result": {"claims": claim_ids, "unknowns": result["unknowns"], "plan_id": plan_id, "status": status,
                   "subquestions": [{k: s.get(k) for k in ("id", "intent", "status", "claim_ids", "flags")
                                     if k in s} for s in result.get("subquestions") or []]},
        "plan_id": plan_id, "created_at": now()})


def audit(store: Store, repo: Path, analysis_id: str, *, refresh: bool = True) -> dict:
    """Re-judge an analysis' sub-questions on the current claim statuses (``verinoda plan audit``).

    With ``refresh`` the index is updated first, so claims whose files changed
    are already marked stale. A sub-question relying on a stale claim is
    reported ``stale`` whatever its recomputed verdict; ``changed`` lists the
    sub-questions whose verdict moved since the analysis.
    """
    row = store.get("analyses", analysis_id)
    if row is None:
        raise KeyError(f"no analysis {analysis_id}")
    refreshed = None
    if refresh:
        from verinoda import workflow

        try:
            res = workflow.update(store, Path(repo).resolve())
            refreshed = {"mode": res.get("mode"), "stale": len(res.get("stale") or []), "error": res.get("error")}
        except Exception as exc:  # noqa: BLE001 - audit still runs on the recorded statuses
            refreshed = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    result = row.get("result") or {}
    plan_id = row.get("plan_id") or result.get("plan_id")
    prow = qp.get_plan(store, plan_id) if plan_id else None
    plan = (prow or {}).get("plan") or {}
    by_id = {sq.get("id"): sq for sq in plan.get("sub_questions") or []}
    out = []
    for s in result.get("subquestions") or []:
        sq = by_id.get(s["id"], {"id": s["id"], "intent": s.get("intent")})
        ids = s.get("claim_ids") or []
        rows = _claim_rows(store, ids)
        stale = [c["id"] for c in rows if c["status"] == "stale"]
        now_status = judge(sq, rows, s.get("flags") or {})
        verdict = "stale" if stale else now_status
        out.append({"id": s["id"], "intent": s.get("intent"), "was": s.get("status"), "now": verdict,
                    "recomputed": now_status, "stale_claims": stale,
                    "claims": [{"id": c["id"], "status": c["status"], "kind": c["kind"]} for c in rows]})
    return {"analysis_id": analysis_id, "plan_id": plan_id, "question": row.get("question"),
            "refreshed": refreshed, "subquestions": out,
            "changed": [s["id"] for s in out if s["now"] != s["was"]]}


def _run_tests(store: Store, repo: Path, g: index.Graph, it: dict, test_nodes: list[str], commit: str | None,
               budget: Budget, rec: _Recorder, step, unknown, cfg_all: dict) -> None:
    """Run the Python tests that statically reach ``it`` and record the outcome as a claim."""
    from verinoda import experiments

    py_tests = [n for n in test_nodes if (g.file(n) or "").endswith(".py")]
    question = f"do the tests reaching {it['symbol']} pass?"
    if not py_tests:
        unknown({"question": question, "why": "no Python test function reaches it (only Python/pytest tests are run)",
                 "next_step": "run the project's own test runner manually"})
        return
    remaining = budget.remaining_seconds()
    if remaining < 1 or not budget.work_ok():
        rec.skipped["tests"] += 1
        return
    timeout = min(float(cfg_all["experiments"]["default_timeout"]), remaining)
    ids = list(dict.fromkeys(_pytest_id(g, n) for n in py_tests[:5]))
    python = getattr(experiments, "python_for", None)
    exe = python(repo) if python else sys.executable
    try:
        res = experiments.run(store, repo, [exe, "-m", "pytest", "-q", *ids],
                              hypothesis=f"tests reaching {it['symbol']} pass at {(commit or '')[:10]}",
                              claim_id=None, commit=commit, timeout=timeout)
    except Exception as exc:  # refused or could not start
        unknown({"question": question, "why": str(exc), "next_step": "run them manually or enable container isolation"})
        return
    step("experiment", f"{res['outcome']} in {res['duration_s']}s ({res['isolation']}, timeout {timeout:.0f}s)")
    # The run depends on the test files too: editing them must make this
    # claim stale like editing the code under test.
    test_files = sorted({i.split("::")[0] for i in ids})
    text = f"Tests that reach `{it['symbol']}` pass: {', '.join(ids)}"
    subjects = [f"{it['file']}::{it['symbol']}", *test_files]
    spec = {"command": ids, "experiment": res["id"]}
    if res["outcome"] in ("inconclusive", "timeout"):
        why = (res.get("inconclusive_reason") or
               (f"timed out after {timeout:.0f}s" if res["outcome"] == "timeout" else "pytest could not tell"))
        # Says nothing about pass or fail: never contradicts the claim, only qualifies it.
        rec.claim(text, kind="test_run", status="unknown", subjects=subjects,
                  evidence=[(res["evidence_id"], "qualifies")], spec=spec,
                  uncertainties=[f"test run {res['outcome']}: {why}",
                                 res.get("next_step") or "fix the test environment and re-run with --run"])
        return
    rc = rec.claim(text, kind="test_run", status="experiment_verified", subjects=subjects,
                   evidence=[(res["evidence_id"], "supports" if res["matches_expectation"] else "refutes")], spec=spec)
    if rc is not None and not res["matches_expectation"]:
        Claims(store, repo).reassess(rc["id"], reason="experiment failed")
