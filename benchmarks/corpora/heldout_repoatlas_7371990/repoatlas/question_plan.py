"""Question plans: what the user asked, checked against the code before any work.

Design: docs/DESIGN.md D1-D4 and D8. A plan (``repoatlas.question_plan/1``,
schema in ``repoatlas/schemas/question_plan.v1.json``) restates the user's
message as sub-questions with intents and checkable ``done_when`` criteria,
the words that name code (mentions) and the references with the version the
user meant. The host agent may write it; otherwise :func:`draft` builds the
same object from rules, so analysis always runs through a plan.

:func:`check` is deterministic and offline (local git at most). In order:

1. **schema** - a validator for the flat schema subset (type, enum, const,
   required, properties, additionalProperties, items, pattern, min/max);
2. **integrity** - unique ids, every referenced id exists, ``depends_on`` plus
   ``subject_from`` form a DAG (Kahn), limits (6 sub-questions, 20 mentions,
   10 references, 5 candidates);
3. **grounding** - every mention/reference text occurs verbatim in the user
   message (NFC, Turkish fold, case fold, apostrophes unified) or, for
   ``source=conversation``, in ``conversation_context``;
4. **versions** - every version-like token in the message (``v0.3``, a SHA,
   ``#12``, ``PR 12``, ``2.31`` next to a version word) is carried by some
   reference (``version_dropped`` otherwise); relative version words
   ("eski sürüm", "previous version") need ``version.source=user_relative``
   and produce a version clarification;
5. **cross-checks** (warnings, never overrides) - intent vs ``done_when``
   kind, language, and the rule-based intents (``intent_divergence``);
6. **mention linking** (:func:`link_mentions`) in evidence tiers - exact id
   1.00, path 0.95, ``Class.method`` 0.95, exact label 0.95, folded label
   0.85, identifier parts 0.75, fuzzy (rapidfuzz ratio >= 90) 0.60, lexicon
   0.5 x association, seed dictionary 0.45, text hit 0.40. Host candidates
   that match nothing are rejected and never used as seeds. Status: *linked*
   (>= 0.70 and 0.15 ahead of the next family), *ambiguous*, *weak*
   (0.40-0.70; claims carry the uncertainty) or *unlinked* (unknown + next
   step). Before an ambiguous entity is asked about, a graph probe checks
   whether the choice changes the answer; if not, the families are merged
   silently (``family_merged``). Domain concepts ("order", "sipariş") are
   expected to match many names and are always merged;
7. **clarifications** - at most 3, from fixed Turkish/English templates, with
   options drawn only from grounded candidates plus ``__all__``/``__other__``.

Status: ``invalid`` (errors), ``needs_clarification`` or ``ready``.

:func:`draft` segments the message (a clause becomes a sub-question only when
it has its own question or intent cue), reads intents from bilingual cue
tables (with *domain shadowing*: a Turkish cue that is also a domain word of
this repository - "gider", "yazar", "kayıt" - needs a second cue), maps
Turkish case suffixes to roles (ablative = source, dative = target),
resolves anaphora ("bunu", "it") through ``subject_from``, and tags every
element with ``derived_by``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from functools import lru_cache
from importlib import resources
from pathlib import Path, PurePosixPath

from repoatlas import textnorm as tn

SCHEMA_ID = "repoatlas.question_plan/1"
CHECK_SCHEMA_ID = "repoatlas.plan_check/1"
DRAFT_RULES = "question_plan.draft/1"

INTENTS = ("locate", "define", "flow", "callers", "dataflow", "config", "tests", "why", "history", "impact",
           "behaviour", "compare_reference", "performance", "architecture", "usage")
DONE_KINDS = ("location_verified", "path_found", "set_enumerated", "claim_exists", "decision_found",
              "proposition_checked", "reference_pinned", "comparison_done")
LIMITS = {"sub_questions": 6, "mentions": 20, "references": 10, "candidates": 5}
THRESHOLDS = {"link": 0.70, "weak": 0.40, "margin": 0.15, "fuzzy": 90, "did_you_mean": 85,
              "max_clarifications": 3}
TIER_SCORES = {"exact_id": 1.0, "path": 0.95, "qualified": 0.95, "exact_label": 0.95, "label_folded": 0.85,
               "identifier_parts": 0.75, "fuzzy": 0.60, "seed_dictionary": 0.45, "text_hit": 0.40}
LEXICON_FACTOR = 0.5     # a lexicon match scores 0.5 x its association score
NAME_TIERS = frozenset({"exact_id", "path", "qualified", "exact_label", "label_folded"})
ENTITY_KINDS = frozenset({"symbol", "file", "module", "config_key", "env_var", "test", "cli_command", "endpoint",
                          "db_table", "error_message"})

# intent -> (default done_when kind, min_status, detail)
DEFAULT_DONE: dict[str, tuple[str, str, str]] = {
    "locate": ("location_verified", "statically_verified", "file:line of the definition the sub-question is about"),
    "define": ("location_verified", "statically_verified", "file:line of the definition, with what it does"),
    "flow": ("path_found", "strong_inference", "a directed call path between the subjects, every hop with file:line"),
    "callers": ("set_enumerated", "strong_inference", "the callers with call-site lines, or an explicit none-found"),
    "dataflow": ("claim_exists", "strong_inference", "the path that takes the data to persistence, with file:line"),
    "config": ("claim_exists", "statically_verified", "the configuration reads that decide it, with file:line"),
    "tests": ("set_enumerated", "strong_inference", "tests reaching the subject, or an explicit none-found"),
    "why": ("decision_found", "primary_source_verified", "a decision record or commit that states the reason"),
    "history": ("decision_found", "primary_source_verified", "the commits that changed it"),
    "impact": ("set_enumerated", "strong_inference", "the code and tests that depend on the subject"),
    "behaviour": ("proposition_checked", "statically_verified", "the proposition checked against the code"),
    "compare_reference": ("comparison_done", "primary_source_verified", "both versions pinned and compared"),
    "performance": ("claim_exists", "strong_inference", "code that determines the cost, with file:line"),
    "architecture": ("claim_exists", "strong_inference", "the modules and their dependencies"),
    "usage": ("claim_exists", "strong_inference", "an entry point or example showing the use"),
}
INTENT_DONE_OK: dict[str, frozenset[str]] = {
    "locate": frozenset({"location_verified", "claim_exists"}),
    "define": frozenset({"location_verified", "claim_exists"}),
    "flow": frozenset({"path_found", "claim_exists"}),
    "callers": frozenset({"set_enumerated", "claim_exists"}),
    "dataflow": frozenset({"claim_exists", "path_found"}),
    "config": frozenset({"claim_exists", "location_verified", "set_enumerated"}),
    "tests": frozenset({"set_enumerated", "claim_exists"}),
    "why": frozenset({"decision_found", "claim_exists"}),
    "history": frozenset({"decision_found", "claim_exists"}),
    "impact": frozenset({"set_enumerated", "claim_exists"}),
    "behaviour": frozenset({"proposition_checked", "claim_exists"}),
    "compare_reference": frozenset({"comparison_done", "reference_pinned"}),
    "performance": frozenset({"claim_exists"}),
    "architecture": frozenset({"claim_exists"}),
    "usage": frozenset({"claim_exists", "location_verified"}),
}
INTENT_NAMES_TR = {
    "locate": "konum", "define": "tanım", "flow": "akış", "callers": "çağıranlar", "dataflow": "veri yolu",
    "config": "yapılandırma", "tests": "testler", "why": "gerekçe", "history": "geçmiş", "impact": "etki",
    "behaviour": "davranış", "compare_reference": "karşılaştırma", "performance": "performans",
    "architecture": "mimari", "usage": "kullanım",
}

# -- schema ---------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def schema() -> dict:
    """The packaged JSON Schema of ``repoatlas.question_plan/1``."""
    raw = resources.files("repoatlas").joinpath("schemas/question_plan.v1.json").read_text(encoding="utf-8")
    return json.loads(raw)


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float)}


def _type_ok(value, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def _validate(value, sch: dict, at: str, out: list[dict]) -> None:
    if "const" in sch and value != sch["const"]:
        out.append(_problem(at, "schema", f"must be {sch['const']!r}", f"set it to {sch['const']!r}"))
        return
    if "enum" in sch and value not in sch["enum"]:
        out.append(_problem(at, "schema", f"{value!r} is not one of {sch['enum']}", "use one of the listed values"))
        return
    t = sch.get("type")
    if t and not _type_ok(value, t):
        out.append(_problem(at, "schema", f"expected {t}, got {type(value).__name__}", f"send a {t}"))
        return
    if isinstance(value, str):
        if len(value) < sch.get("minLength", 0):
            out.append(_problem(at, "schema", "must not be empty", "fill it in"))
        if "pattern" in sch and not re.search(sch["pattern"], value):
            out.append(_problem(at, "schema", f"{value!r} does not match {sch['pattern']}", "use ids like q1, m1, r1"))
    elif isinstance(value, list):
        if len(value) < sch.get("minItems", 0):
            out.append(_problem(at, "schema", f"needs at least {sch['minItems']} item(s)", "add the missing items"))
        if "maxItems" in sch and len(value) > sch["maxItems"]:
            out.append(_problem(at, "limit", f"at most {sch['maxItems']} item(s) allowed", "keep the best ones"))
        if "items" in sch:
            for i, item in enumerate(value):
                _validate(item, sch["items"], f"{at}/{i}", out)
    elif isinstance(value, dict):
        props = sch.get("properties", {})
        for req in sch.get("required", ()):
            if req not in value:
                out.append(_problem(f"{at}/{req}", "schema", "required field is missing", f"add '{req}'"))
        extra = sch.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], f"{at}/{k}", out)
            elif extra is False:
                out.append(_problem(f"{at}/{k}", "schema", "unknown field", "remove it (see `repoatlas plan schema`)"))
            elif isinstance(extra, dict):
                _validate(v, extra, f"{at}/{k}", out)


def _problem(at: str, code: str, msg: str, fix: str = "") -> dict:
    return {"at": at or "/", "code": code, "msg": msg, "fix": fix}


def validate(plan) -> list[dict]:
    """Schema problems of a plan object (``[]`` when it conforms)."""
    out: list[dict] = []
    _validate(plan, schema(), "", out)
    return out


def parse(obj) -> tuple[dict | None, list[dict]]:
    """A plan from a dict, a JSON string or a path to a JSON file; ``(plan, problems)``."""
    if isinstance(obj, dict):
        plan = obj
    else:
        text = obj
        if isinstance(obj, Path) or (isinstance(obj, str) and not obj.lstrip().startswith("{")
                                     and len(obj) < 1024 and Path(obj).is_file()):
            try:
                text = Path(obj).read_text(encoding="utf-8-sig")
            except OSError as exc:
                return None, [_problem("/", "schema", f"cannot read plan file: {exc}", "check the path")]
        try:
            plan = json.loads(text)
        except (TypeError, ValueError) as exc:
            return None, [_problem("/", "schema", f"not valid JSON: {exc}",
                                   "write the plan to a file under .repoatlas/plans/ instead of the command line")]
    return (plan if isinstance(plan, dict) else None), validate(plan)


def plan_hash(plan: dict) -> str:
    """``sha256:<hex>`` of the canonical JSON (sorted keys, compact separators)."""
    canon = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


# -- intent cues ----------------------------------------------------------------------------------

EN_CUES: dict[str, list[str]] = {
    "callers": [r"\bwho calls\b", r"\bwhat calls\b", r"\bcallers?\b", r"\bcalled (by|from)\b", r"\bused by\b",
                r"\bcall ?sites?\b", r"\bwhere is .+ (called|used)\b",
                r"\bwhich (functions?|methods?|code|modules?|classes?) (calls?|uses?)\b"],
    "performance": [r"\bslow\w*", r"\bperformance\b", r"\blatency\b", r"\bfaster\b", r"\bbottlenecks?\b",
                    r"\bspeed\b"],
    "why": [r"\bwhy\b", r"\brationale\b", r"\breasons?\b", r"\bdecided to\b", r"\bdecision\b", r"\bmotivation\b"],
    "history": [r"\bwhen (was|were|did)\b", r"\bwho (changed|wrote|added|introduced|removed)\b",
                r"\bwhich (commit|pr|pull request)\b", r"\bhistory\b", r"\bsince when\b"],
    "impact": [r"\bimpact\w*", r"\baffect\w*", r"\bbreak(s|ing)?\b", r"\bchang(e|es|ed|ing)\b"],
    "tests": [r"\btest(s|ed|ing)?\b", r"\bcoverage\b", r"\bcover(s|ed)?\b", r"\bexercis(e|es|ed)\b",
              r"\bretest\w*"],
    "compare_reference": [r"\bdiffer\w*", r"\bcompar\w*", r"\bvs\.?(?=\s)", r"\bversus\b"],
    "config": [r"\bconfig\w*", r"\benv\b", r"\benvironment\b", r"\bsettings?\b", r"\bvariables?\b",
               r"\b(controls?|determines?|decides?)\b", r"\boptions?\b", r"\bflags?\b"],
    "flow": [r"\bflows?\b", r"\bpath\b", r"\breach(es|ed)?\b", r"\btrace\b", r"\bfrom\b.+\bto\b",
             r"\bhow (does|do)\b", r"\bcall (chain|graph|path|stack)\b", r"\bcalls?\b", r"\bcalled\b"],
    "behaviour": [r"\bwhat happens (when|if)\b", r"\bbefore\b", r"\bafter\b",
                  r"^\s*(does|do|is|are|can|will|should|could|would|did|was|were)\b", r"\bwhat if\b"],
    "locate": [r"\bwhere\b", r"\bwhich (files?|modules?|functions?|classes?|methods?|lines?|packages?)\b",
               r"\bdefined\b", r"\blocated\b", r"\bin which (file|module)\b"],
    "dataflow": [r"\bpersist\w*", r"\bstor(e|ed|es|ing|age)\b", r"\bdatabases?\b", r"\bdb\b", r"\bsql\b",
                 r"\bsav(e|es|ed|ing)\b", r"\bwrit(e|es|ten|ing)\b", r"\binsert\w*"],
    "define": [r"\bwhat (is|are)\b", r"\bmeans?\b", r"\bmeaning\b", r"\bwhat does .+ do\b", r"\bpurpose of\b"],
    "usage": [r"\bhow (do|can|should) (i|we|you) (use|call|run|configure)\b", r"\bexamples?\b", r"\busage\b"],
    "architecture": [r"\barchitecture\b", r"\boverview\b", r"\bstructure\b", r"\blayers?\b"],
}
# Weak cues count only when no other cue fires in the clause.
EN_WEAK: dict[str, list[str]] = {}
TR_WEAK: dict[str, list[str]] = {"flow": [r"\bnasil\b"]}
# (pattern, shadowable): a shadowable cue is also a common domain noun; it needs a
# second cue of the same intent when the word is part of this repository's vocabulary.
TR_CUES: dict[str, list[tuple[str, bool]]] = {
    "callers": [(r"\bkim(ler)? (cagir|kullan)\w*", False), (r"\bnereden cagri\w*|\bnereden cagril\w*", False),
                (r"\bcagiran\w*", False), (r"\bkullanan\w*", False), (r"\bkullanildig\w*", False),
                (r"\bcagrildig\w*", False),
                (r"\bhangi (fonksiyon|metot|metod|modul|sinif)\w* (cagir|kullan)\w*", False)],
    "performance": [(r"\byavas\w*", False), (r"\bperformans\w*", False), (r"\bhizli\w*", False),
                    (r"\bgecikme\w*", False)],
    "why": [(r"\bneden\b(?!\s+ol)", False), (r"\bnicin\b", False), (r"\bniye\b", False), (r"\bgerekce\w*", False),
            (r"\bkarar\w*", False), (r"\bsebe[bp]\w*", False)],
    "history": [(r"\bne zaman\b", False), (r"\bkim(ler)? (degistir|yazdi|ekledi|sildi)\w*", False),
                (r"\bhangi commit\w*", False), (r"\bgecmis\w*", False)],
    "impact": [(r"\betki\w*", False), (r"\bdegis(?!ken)\w*", False), (r"\bboz\w*", False)],
    "tests": [(r"\btest\w*", False), (r"\bkapsa\w*", False), (r"\bsina(ma|n)\w*", False)],
    "compare_reference": [(r"\bfark\w*", False), (r"\bkarsilastir\w*", False), (r"\bbizimki\w*", False)],
    "config": [(r"\byapilandir\w*", False), (r"\bayar\w*", False), (r"\bortam degisken\w*", False),
               (r"\bortam\b", False), (r"\bbelirl(e|iyor|er|en|ey)\w*", False), (r"\bkontrol ed\w*", False),
               (r"\bdegisken\w*", False), (r"\bkonfig\w*", False)],
    "flow": [(r"\bulas\w*", False), (r"\bakis\w*", False), (r"\bakar\b", False),
             (r"\bgec(iyor|ir|er|ti|tig)\w*", False), (r"\bgidiyor\b|\bgit(ti|tig|mesi|mek)\w*", False),
             (r"\bgider\b", True), (r"\bcagri (yol|zincir)\w*", False), (r"\bcagir\w*", False),
             (r"\bcagri\w*", False)],
    "behaviour": [(r"\bm[iu]\b", False), (r"\bm[iu](dir|sun|yim|yiz)\b", False), (r"\bonce\b", False),
                  (r"\bsonra\b", False), (r"\bolursa\b", False), (r"\bne olu(r|yor)\w*", False)],
    "locate": [(r"\bnere\w*", False),
               (r"\bhangi (dosya|modul|fonksiyon|sinif|metot|metod|satir|paket|klasor)\w*", False),
               (r"\btanimli\w*|\btanimlan\w*|\btanimi\b", False)],
    "dataflow": [(r"\bveritaban\w*", False), (r"\bkalici\w*", False), (r"\bkayde\w*|\bkaydet\w*", False),
                 (r"\byaz(?!ilim|dir|ar\b)\w*", False), (r"\byazar\b", True), (r"\bkayit\w*|\bkaydi\w*", True),
                 (r"\bdepola\w*", False), (r"\bsakla\w*", False)],
    "define": [(r"\bnedir\b", False), (r"\bne ise yarar\b", False), (r"\bne yapar\b", False),
               (r"\bne demek\w*", False), (r"\banlami\w*", False)],
    "usage": [(r"\bnasil kullan\w*", False), (r"\bornek\w*", False), (r"\bkullanim\w*", False)],
    "architecture": [(r"\bmimari\w*", False), (r"\bgenel bakis\w*", False), (r"\bkatman\w*", False)],
}
# Primary-intent precedence when a clause carries several cues.
PRIORITY = ("callers", "performance", "why", "history", "impact", "tests", "compare_reference", "config", "usage",
            "flow", "behaviour", "locate", "dataflow", "define", "architecture")
_EN_RX = {k: [re.compile(p) for p in v] for k, v in EN_CUES.items()}
_EN_WEAK_RX = {k: [re.compile(p) for p in v] for k, v in EN_WEAK.items()}
_TR_WEAK_RX = {k: [re.compile(p) for p in v] for k, v in TR_WEAK.items()}
_TR_RX = {k: [(re.compile(p), s) for p, s in v] for k, v in TR_CUES.items()}

EN_INTERROGATIVES = frozenset("what where which how why who whom whose when does do is are can should could would "
                              "will did was were".split())
TR_INTERROGATIVES = frozenset("ne neyi neler nedir nerede nereye nereden neresi hangi hangisi hangileri nasil neden "
                              "nicin niye kim kimi kime kimin mi mu midir mudur".split())
EN_ANAPHORS = frozenset("it its this that these those they them their".split())
TR_ANAPHORS = frozenset("bunu onu sunu bunlar onlar bunlari onlari sunlari bunun onun sunun buna ona suna bu su o "
                        "bunda onda".split())
_ABLATIVE = ("den", "dan", "ten", "tan", "nden", "ndan")
_DATIVE_TAIL = ("ine", "ina", "ye", "ya")  # dative endings safe to read without an apostrophe


def _uses_turkish_cues(text: str) -> bool:
    """Turkish letters, two Turkish function words, or one Turkish question word ("kayit nerede")."""
    if tn.has_turkish(text):
        return True
    return any(w in tn.TR_QUESTION_WORDS for w in re.findall(r"[a-z]+", tn.fold_tr(text)))


def _shadowed(word: str, lexicon) -> bool:
    """Is a Turkish cue word also a domain word of this repository?"""
    if lexicon is None:
        return False
    if lexicon.has(word):
        return True
    return any(hit["targets"] for hit in lexicon.seed([word]))


def clause_cues(text: str, lexicon=None) -> list[dict]:
    """Intent cues found in one clause: ``[{intent, cue, lang}]`` in :data:`PRIORITY` order."""
    low = tn.nfc(text).lower()
    found: dict[str, dict] = {}
    for intent, rxs in _EN_RX.items():
        for rx in rxs:
            m = rx.search(low)
            if m:
                found[intent] = {"intent": intent, "cue": m.group(0).strip(), "lang": "en"}
                break
    if _uses_turkish_cues(text):
        folded = tn.fold_tr(tn.nfc(text))
        for intent, rxs in _TR_RX.items():
            if intent in found:
                continue
            hits = [(m, shadow) for rx, shadow in rxs for m in [rx.search(folded)] if m]
            strong = [m for m, shadow in hits if not shadow or not _shadowed(m.group(0), lexicon)]
            if strong:
                found[intent] = {"intent": intent, "cue": strong[0].group(0), "lang": "tr"}
            elif len(hits) >= 2:  # two shadowable cues back each other up
                found[intent] = {"intent": intent, "cue": hits[0][0].group(0), "lang": "tr"}
        if "flow" not in found and _case_pair(folded):
            found["flow"] = {"intent": "flow", "cue": "ablative+dative", "lang": "tr"}
    if not found:
        weak = [(_EN_WEAK_RX, low, "en")]
        if _uses_turkish_cues(text):
            weak.append((_TR_WEAK_RX, tn.fold_tr(tn.nfc(text)), "tr"))
        for table, hay, lang in weak:
            for intent, rxs in table.items():
                m = next((m for rx in rxs for m in [rx.search(hay)] if m), None)
                if m and intent not in found:
                    found[intent] = {"intent": intent, "cue": m.group(0).strip(), "lang": lang, "weak": True}
    return [found[i] for i in PRIORITY if i in found]


def _case_pair(folded: str) -> bool:
    """An ablative word followed later by a dative one ("API'den veritabanına")."""
    toks = re.findall(r"[a-z0-9_.]+(?:'[a-z]+)?", folded)
    abl = None
    for i, t in enumerate(toks):
        base, _, suf = t.partition("'")
        if suf:
            role = tn.suffix_role(suf)
        elif len(base) > 5:
            role = "source" if base.endswith(_ABLATIVE) else "target" if base.endswith(_DATIVE_TAIL) else None
        else:
            role = None
        if role == "source":
            abl = i
        elif role == "target" and abl is not None and i > abl:
            return True
    return False


def intents_for(text: str, lexicon=None) -> list[str]:
    """Rule-based intents of a whole message (all clauses), in :data:`PRIORITY` order."""
    got: set[str] = set()
    for cl in segment(text):
        got.update(c["intent"] for c in clause_cues(cl["text"], lexicon))
    return [i for i in PRIORITY if i in got]


_WH = frozenset("what where which how why who when".split())


def _interrogative(text: str) -> bool:
    """A question mark, a leading English question word, or a wh-/Turkish question word anywhere."""
    if text.rstrip().endswith("?"):
        return True
    words = re.findall(r"[a-z]+", tn.fold_tr(text).lower())
    return bool(words) and (words[0] in EN_INTERROGATIVES
                            or any(w in TR_INTERROGATIVES or w in _WH for w in words))


# -- segmentation ---------------------------------------------------------------------------------

_SPLITS = [
    re.compile(r"\?\s+|;\s*|!\s+|\n+"),
    re.compile(r",?\s+(?:and|also|then)\s+(?=(?:what|where|which|how|why|who|when|does|do|is|are|can|should|"
               r"will|did)\b)"),
    re.compile(r",\s*(?:also|then)\s+"),
    re.compile(r",\s+(?=(?:what|where|which|how|why|who|when)\b)"),
    re.compile(r"\s+ve\s+(?=(?:\w+\s+)?(?:ne|neyi|neler|nerede|nereye|nereden|hangi|hangisi|nasil|neden|nicin|"
               r"niye|kim|kimi|kime)\b)"),
    re.compile(r",\s*(?:ayrica|bir de|peki)\s+"),
    re.compile(r"\s+peki\s+|\s+bir de\s+|\s+hem de\s+"),
    re.compile(r",\s+(?=\w+(?:ysa|yse|sa|se)\b)"),
    re.compile(r",\s+(?=(?:ne|neyi|nerede|nereye|nereden|hangi|nasil|neden|kim)\b)"),
]
_CONDITIONAL = re.compile(r"^\s*\w+(?:ysa|yse|sa|se)\b|^\s*if (?:so|yes|it does|they do)\b")
_TR_VE = re.compile(r"\s+ve\s+")


def _has_question_word(folded: str) -> bool:
    return any(w in TR_INTERROGATIVES or w in _WH for w in re.findall(r"[a-z]+", folded))


def segment(message: str) -> list[dict]:
    """Clauses of a message: ``[{text, start, end, conditional}]`` (offsets into ``message``).

    Split points are found on the folded text (same length as the message).
    A piece becomes its own clause only when it has its own question word or
    intent cue; otherwise it is merged back into the previous clause, so noun
    phrases like "sipariş ve fatura" stay whole.
    """
    folded = tn.fold_tr(message).lower()
    cuts: set[tuple[int, int]] = set()
    for rx in _SPLITS:
        for m in rx.finditer(folded):
            if m.end() > m.start() or rx is _SPLITS[0]:
                cuts.add((m.start(), m.end()))
    # Turkish puts the question word late ("... ve veritabanı dosyasını ne belirliyor?"): a
    # bare "ve" splits when the text on both sides of it asks its own question.
    bounds = sorted({0, len(folded)} | {a for a, _ in cuts} | {b for _, b in cuts})
    for m in _TR_VE.finditer(folded):
        lo = max(b for b in bounds if b <= m.start())
        hi = min(b for b in bounds if b >= m.end())
        if _has_question_word(folded[lo:m.start()]) and _has_question_word(folded[m.end():hi]):
            cuts.add((m.start(), m.end()))
    pieces: list[list[int]] = []
    pos = 0
    for a, b in sorted(cuts):
        if a < pos:
            continue
        end = a + (1 if message[a:a + 1] in "?!" else 0)
        if message[pos:end].strip():
            pieces.append([pos, end])
        pos = b
    if message[pos:].strip():
        pieces.append([pos, len(message)])
    # A piece without its own question word or cue joins its predecessor (the
    # first piece joins its successor instead).
    spans: list[list[int]] = []
    carry: int | None = None
    for a, b in pieces:
        has_cue = _interrogative(message[a:b]) or bool(clause_cues(message[a:b]))
        if carry is not None:
            a, carry = carry, None
        if not has_cue and not spans:
            carry = a
        elif not has_cue:
            spans[-1][1] = b
        else:
            spans.append([a, b])
    if carry is not None:
        if spans:
            spans[-1][1] = pieces[-1][1]
        else:
            spans.append([carry, pieces[-1][1]])
    clauses: list[dict] = []
    for a, b in spans:
        text = message[a:b]
        clauses.append({"text": text, "start": a, "end": b,
                        "conditional": bool(clauses) and bool(_CONDITIONAL.search(tn.fold_tr(text).lower()))})
    for c in clauses:  # trim whitespace while keeping offsets exact
        lead = len(c["text"]) - len(c["text"].lstrip())
        trail = len(c["text"]) - len(c["text"].rstrip())
        c["start"] += lead
        c["end"] -= trail
        c["text"] = message[c["start"]:c["end"]]
    return clauses or [{"text": message.strip(), "start": 0, "end": len(message), "conditional": False}]


# -- graph index for linking -------------------------------------------------------------------

_PROSE_TYPES = ("document", "rationale")
_CODE_EXTS = {"py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "rb", "md", "rst", "json", "toml", "yaml", "yml",
              "txt", "cfg", "ini", "html", "css", "sql", "sh", "c", "h", "cpp", "hpp", "cs", "kt", "swift", "php",
              "lua", "scala"}


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", tn.fold_tr(s))


def _bare(label: str) -> str:
    return label.strip().lstrip(".").split("(")[0].strip()


def _stems(parts) -> frozenset[str]:
    return frozenset(tn.en_stem(p) for p in parts)


class _Index:
    """Lookups over a graph's nodes (labels, folded labels, identifier parts, paths); see :func:`_index`."""

    def __init__(self, g):
        self.n = len(g.G)
        self.by_bare: dict[str, list[str]] = defaultdict(list)
        self.by_compact: dict[str, list[str]] = defaultdict(list)
        self.by_part: dict[str, set[str]] = defaultdict(set)
        self.files: dict[str, str] = {}
        self.by_base: dict[str, list[str]] = defaultdict(list)
        self.parts: dict[str, frozenset[str]] = {}
        self.prose: set[str] = set()
        self.compact_labels: list[str] = []
        self.compact_owner: list[str] = []
        for n, d in g.G.nodes(data=True):
            f = d.get("source_file")
            if not f:
                continue
            label = str(d.get("label") or n)
            ftype = d.get("file_type")
            if g.is_file_node(n):
                self.files[f] = n
                self.by_base[PurePosixPath(f).name].append(f)
            if ftype in _PROSE_TYPES:
                self.prose.add(n)
            bare = _bare(label)
            self.by_bare[bare].append(n)
            cmp_ = _compact(bare)
            if cmp_:
                self.by_compact[cmp_].append(n)
                if ftype not in _PROSE_TYPES:
                    self.compact_labels.append(cmp_)
                    self.compact_owner.append(n)
            parts = _stems(p for p in tn.split_identifier(bare.replace(".", "_").replace("-", "_"))
                           if len(p) >= 2)
            self.parts[n] = parts
            for p in parts:
                self.by_part[p].add(n)


_INDEX_CACHE: dict[tuple, _Index] = {}


def _index(g) -> _Index:
    """The lookup index of a graph, shared by every load of the same graph file."""
    try:
        stamp = Path(g.path).stat().st_mtime_ns
    except (OSError, TypeError):
        stamp = id(g)  # an in-memory graph: key on the object
    key = (str(g.path), stamp, len(g.G), g.G.number_of_edges())
    ix = _INDEX_CACHE.get(key)
    if ix is None:
        if len(_INDEX_CACHE) > 8:
            _INDEX_CACHE.clear()
        ix = _INDEX_CACHE[key] = _Index(g)
    return ix


def _lexicon_for(graph, lexicon):
    if lexicon is not None:
        return lexicon
    from repoatlas import lexicon as lexmod

    return lexmod.from_graph(graph)


# -- matching one candidate string ---------------------------------------------------------------

def _match_string(g, ix: _Index, cand: str) -> list[tuple[str, float, str]]:
    """Graph nodes a candidate string names: ``[(node, score, match type)]``."""
    out: dict[str, tuple[float, str]] = {}

    def put(n: str, tier: str, score: float | None = None) -> None:
        s = TIER_SCORES[tier] if score is None else score
        if n in g.G and (n not in out or s > out[n][0]):
            out[n] = (s, tier)

    c = cand.strip().strip("`'\"").rstrip("?.,;:!")
    if not c:
        return []
    if c.endswith("()"):
        c = c[:-2]
    if c in g.G and g.file(c):
        put(c, "exact_id")
    if "::" in c:
        f, _, sym = c.partition("::")
        for n in g.symbols_in(f.replace("\\", "/")):
            if _bare(g.label(n)) == _bare(sym.split("::")[-1]):
                put(n, "exact_id")
    posix = c.replace("\\", "/").lstrip("./")
    suffix = PurePosixPath(posix).suffix[1:].lower()
    if "/" in posix or suffix in _CODE_EXTS:
        if posix in ix.files:
            put(ix.files[posix], "path")
        else:
            for f in ix.by_base.get(PurePosixPath(posix).name, ()):
                if f.endswith("/" + posix) or f == posix or "/" not in posix:
                    put(ix.files[f], "path")
    if "." in c and "/" not in c and suffix not in _CODE_EXTS:
        cls, _, meth = c.rpartition(".")
        cls = cls.rpartition(".")[2]
        for n in ix.by_bare.get(cls, ()):
            if g.G.nodes[n].get("_callable_class") or g.G.nodes[n].get("file_type") == "code":
                for v, _ in g.out_edges(n, {"method"}):
                    if _bare(g.label(v)) == meth:
                        put(v, "qualified")
    bare = _bare(c)
    for n in ix.by_bare.get(bare, ()):
        put(n, "exact_label" if n not in ix.prose else "text_hit")
    cmp_ = _compact(bare)
    if cmp_:
        for n in ix.by_compact.get(cmp_, ()):
            put(n, "label_folded" if n not in ix.prose else "text_hit")
    parts = [tn.en_stem(p) for p in tn.split_identifier(bare.replace(".", "_").replace("-", "_").replace(" ", "_"))
             if len(p) >= 3 or p in tn.SHORT_TECH]
    if parts:
        pools = [ix.by_part.get(p, set()) for p in parts]
        common = set.intersection(*pools) if pools else set()
        for n in common:
            put(n, "identifier_parts" if n not in ix.prose else "text_hit")
    if len(cmp_) >= 4 and not out:
        try:
            from rapidfuzz import fuzz, process
        except ImportError:  # pragma: no cover - rapidfuzz is a dependency
            return [(n, s, t) for n, (s, t) in out.items()]
        for _, score, i in process.extract(cmp_, ix.compact_labels, scorer=fuzz.ratio,
                                           score_cutoff=THRESHOLDS["fuzzy"], limit=10):
            put(ix.compact_owner[i], "fuzzy")
    return [(n, s, t) for n, (s, t) in out.items()]


def _near_misses(ix: _Index, text: str, limit: int = 4) -> list[tuple[str, float]]:
    cmp_ = _compact(_bare(text))
    if len(cmp_) < 4:
        return []
    try:
        from rapidfuzz import fuzz, process
    except ImportError:  # pragma: no cover
        return []
    hits = process.extract(cmp_, ix.compact_labels, scorer=fuzz.ratio,
                           score_cutoff=THRESHOLDS["did_you_mean"], limit=limit)
    return [(ix.compact_owner[i], score) for _, score, i in hits]


# -- mention linking ------------------------------------------------------------------------------

def _surface_words(text: str) -> list[str]:
    """Folded words of a mention text, apostrophe suffixes removed."""
    out = []
    for tok in tn.raw_tokens(text):
        out += [w for w in re.findall(r"[a-z0-9_]+", tn.fold_tr(tok)) if w]
    return out


def _site(g, n: str) -> str | None:
    f, ln = g.file(n), g.line(n)
    return f"{f}:{ln}" if f and ln else f


def _at(g, n: str) -> str | None:
    f = g.file(n)
    sp = g.span(n) if f else None
    if f and sp:
        return f"{f}:{sp[0]}-{sp[1]}"
    return _site(g, n)


def _site_hash(g, site: str | None) -> str | None:
    if not site or ":" not in site:
        return None
    from repoatlas import evidence as evmod
    from repoatlas.index import _file_lines

    f, _, ln = site.rpartition(":")
    lines = _file_lines(g.root / f)
    if not lines or not ln.isdigit() or not 0 < int(ln) <= len(lines):
        return None
    return evmod.content_hash(lines[int(ln) - 1])


def _candidate_strings(mention: dict, lexicon) -> list[tuple[str, str, float]]:
    """``(string, via, cap)``: what a mention may name, with the highest score each route allows."""
    out: list[tuple[str, str, float]] = []
    text = mention.get("text") or ""
    base = " ".join(tn.split_apostrophe(t)[0] for t in text.split())
    out.append((base, "text", 1.0))
    words = _surface_words(text)
    if len(words) > 1:
        out.append(("_".join(words), "text", 1.0))
    folded_words = [tn.fold_tr(w) for w in words]
    stems = set()
    for w in folded_words:
        for s in tn.tr_stem_candidates(w)[1:]:
            if lexicon.has(s):
                stems.add(s)
                break
    out += [(s, "text", 1.0) for s in sorted(stems)]
    for c in mention.get("candidates") or []:
        out.append((c, "candidate", 1.0))
    gloss = (mention.get("gloss_en") or "").strip()
    # A rule-drafted gloss comes from the seed dictionary: it must not be
    # laundered into a stronger tier than the seed route below gives it.
    if gloss and not str(mention.get("derived_by") or "").startswith(DRAFT_RULES):
        gw = [w for w in re.findall(r"[a-z0-9_]+", gloss.lower()) if w not in tn.EN_STOPWORDS]
        if gw:
            out.append(("_".join(gw), "gloss_en", 1.0))
        out += [(w, "gloss_en", 1.0) for w in gw if len(gw) > 1]
    for hit in lexicon.seed(folded_words):
        for t in hit["targets"]:
            out.append((t, f"seed_dictionary:{hit['key']}", TIER_SCORES["seed_dictionary"]))
    for w in folded_words:
        if lexicon.has(w):
            continue  # the repository already uses the word itself
        for pr in lexicon.associations(w)[:5]:
            out.append((pr["part"], f"lexicon:{pr['key']}", round(LEXICON_FACTOR * pr["score"], 3)))
    seen = set()
    uniq = []
    for s, via, cap in out:
        if s and (s, via) not in seen:
            seen.add((s, via))
            uniq.append((s, via, cap))
    return uniq


def _text_hits(g, ix: _Index, words: list[str], lexicon) -> list[tuple[str, str]]:
    """Nodes whose comments/strings/body use one of the words (lexicon text index)."""
    out = []
    for w in words:
        if len(w) < 3 and w not in tn.SHORT_TECH:
            continue
        for site in lexicon.text_hits(w):
            f, _, ln = site.rpartition(":")
            n = g.symbol_at(f, int(ln)) if ln.isdigit() else None
            n = n or ix.files.get(f)
            if n:
                out.append((n, w))
    return out


def _families(g, ix: _Index, scored: dict[str, dict]) -> list[dict]:
    """Group candidate nodes: containment (file/its symbols, class/its methods), same file + label."""
    parent = {n: n for n in scored}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    nodes = list(scored)
    by_file: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        by_file[g.file(n) or ""].append(n)
    for f, ns in by_file.items():
        fnode = ix.files.get(f)
        labels: dict[str, str] = {}
        for n in ns:
            if fnode in scored and n != fnode:
                union(n, fnode)
            b = _bare(g.label(n))
            if b in labels:
                union(n, labels[b])
            else:
                labels[b] = n
            for c, _ in g.in_edges(n, {"method"}):
                if c in scored:
                    union(n, c)
    fam: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        fam[find(n)].append(n)
    out = []
    for members in fam.values():
        members.sort(key=lambda n: (-scored[n]["score"], 0 if g.is_symbol(n) else 1, g.line(n) or 0, n))
        out.append({"members": members, "score": scored[members[0]]["score"], "best": members[0]})
    out.sort(key=lambda f: (-f["score"], f["best"]))
    return out


def _cand(g, n: str, info: dict, with_hash: bool) -> dict:
    site = _site(g, n)
    matches = []
    for m in info["matches"][:3]:
        mm = {"type": m["type"], "via": m["via"]}
        if site:
            mm["site"] = site
            if with_hash:
                h = _site_hash(g, site)
                if h:
                    mm["site_hash"] = h
        matches.append(mm)
    return {"node": n, "label": g.label(n), "at": _at(g, n), "score": round(info["score"], 3), "matches": matches}


def _entropy(scores: list[float]) -> float:
    s = [x for x in scores if x >= THRESHOLDS["weak"]]
    if len(s) < 2:
        return 0.0
    tot = sum(s)
    h = -sum((x / tot) * math.log(x / tot) for x in s)
    return round(h / math.log(len(s)), 3)


def link_mention(mention: dict, graph, lexicon=None, *, with_hash: bool = True) -> dict:
    """Ground one mention in the graph (see the module docstring for tiers and statuses)."""
    g = graph
    lex = _lexicon_for(g, lexicon)
    ix = _index(g)
    scored: dict[str, dict] = {}
    rejected = []
    for s, via, cap in _candidate_strings(mention, lex):
        hits = _match_string(g, ix, s)
        if not hits and via == "candidate":
            rejected.append({"candidate": s, "why": "no node, file or label in the graph matches it"})
        for n, score, tier in hits:
            if via.startswith("seed_dictionary"):
                tier, score = "seed_dictionary", min(score, cap)
            elif via.startswith("lexicon"):
                tier, score = "lexicon", min(score, cap)
            if score <= 0:
                continue
            info = scored.setdefault(n, {"score": 0.0, "matches": []})
            info["matches"].append({"type": tier, "via": f"{via}:{s}" if via in ("candidate", "gloss_en") else via,
                                    "score": score})
            info["score"] = max(info["score"], score)
    words = [w for w in (tn.fold_tr(x) for x in _surface_words(mention.get("text") or ""))]
    gloss_words = re.findall(r"[a-z0-9_]+", (mention.get("gloss_en") or "").lower())
    seed_words = [t for hit in lex.seed(words) for t in hit["targets"]]
    for n, w in _text_hits(g, ix, words + gloss_words + seed_words, lex):
        info = scored.setdefault(n, {"score": 0.0, "matches": []})
        if not any(m["type"] == "text_hit" for m in info["matches"]):
            info["matches"].append({"type": "text_hit", "via": f"text:{w}", "score": TIER_SCORES["text_hit"]})
        info["score"] = max(info["score"], TIER_SCORES["text_hit"])
    for info in scored.values():
        info["matches"].sort(key=lambda m: -m["score"])
    link: dict = {"mention": mention.get("id"), "text": mention.get("text"), "status": "unlinked",
                  "alternatives": [], "rejected_candidates": rejected, "nodes": []}
    fams = _families(g, ix, scored)
    link["entropy"] = _entropy([f["score"] for f in fams])
    if not fams or fams[0]["score"] < THRESHOLDS["weak"]:
        near = _near_misses(ix, mention.get("text") or "")
        if near:
            link["near_misses"] = [{"node": n, "label": g.label(n), "at": _at(g, n), "similarity": round(s, 1)}
                                   for n, s in near]
        if fams:
            link["best"] = _cand(g, fams[0]["best"], scored[fams[0]["best"]], with_hash)
        return link
    best = fams[0]
    link["best"] = _cand(g, best["best"], scored[best["best"]], with_hash)
    link["tier"] = scored[best["best"]]["matches"][0]["type"]
    link["alternatives"] = [_cand(g, f["best"], scored[f["best"]], False) for f in fams[1:4]]
    close = [f for f in fams if f["score"] >= THRESHOLDS["link"]
             and best["score"] - f["score"] < THRESHOLDS["margin"]]
    link["_close"] = [f["best"] for f in close]
    if best["score"] < THRESHOLDS["link"]:
        link["status"] = "weak"
        link["uncertainty"] = f"'{mention.get('text')}' linked only by {link['tier']} (score {best['score']:.2f})"
        link["nodes"] = [f["best"] for f in fams if best["score"] - f["score"] < THRESHOLDS["margin"]][:5]
    elif len(close) == 1:
        link["status"] = "linked"
        link["nodes"] = [best["best"]]
    else:
        link["status"] = "ambiguous"
        link["nodes"] = [f["best"] for f in close][:5]
    return link


def _is_concept(mention: dict) -> bool:
    return mention.get("kind") in ("domain_concept", "other") or (
        mention.get("derived_by", "").startswith(DRAFT_RULES) and mention.get("kind") not in ENTITY_KINDS)


def link_mentions(plan: dict, graph, lexicon=None, *, repo=None) -> list[dict]:
    """Links for every mention of the plan (probing and merging ambiguous ones)."""
    lex = _lexicon_for(graph, lexicon)
    mentions = {m["id"]: m for m in plan.get("mentions") or [] if isinstance(m, dict) and "id" in m}
    uses: dict[str, list[dict]] = defaultdict(list)
    for sq in plan.get("sub_questions") or []:
        for mid in sq.get("mentions") or []:
            uses[mid].append(sq)
    links = []
    by_id = {}
    for mid, m in mentions.items():
        lk = link_mention(m, graph, lex)
        by_id[mid] = lk
        links.append(lk)
    answers = {a.get("clarification_id"): a for a in plan.get("answers") or []}
    for mid, lk in by_id.items():
        m = mentions[mid]
        ans = answers.get(f"c-{mid}")
        if ans:
            _apply_answer(graph, lk, ans)
            continue
        if lk["status"] != "ambiguous":
            continue
        close = lk["_close"]
        if _is_concept(m):
            lk["status"] = "linked"
            lk["family_merged"] = close[:5]
            lk["merge_reason"] = "domain concept: every matching name family is kept"
            continue
        verdict = _probe(graph, close, uses.get(mid, []), mentions, by_id, repo)
        if verdict["same"]:
            lk["status"] = "linked"
            lk["family_merged"] = close[:5]
            lk["merge_reason"] = verdict["why"]
        elif verdict.get("pick"):
            lk["status"] = "linked"
            lk["nodes"] = [verdict["pick"]]
            lk["probe_selected"] = verdict["why"]
        else:
            lk["probe"] = verdict["why"]
    for lk in links:
        lk.pop("_close", None)
    return links


def _apply_answer(g, lk: dict, ans: dict) -> None:
    choice = ans.get("choice")
    lk["answered"] = {"choice": choice, "answered_by": ans.get("answered_by")}
    if choice == "__all__":
        lk["status"] = "linked"
        lk["family_merged"] = lk.get("_close") or lk.get("nodes")
        lk["merge_reason"] = "the user chose all candidates"
    elif choice == "__other__" or choice not in g.G:
        lk["status"] = "unlinked"
        lk["nodes"] = []
    else:
        lk["status"] = "linked"
        lk["nodes"] = [choice]


def _probe(g, close: list[str], sqs: list[dict], mentions: dict, links: dict, repo) -> dict:
    """Would choosing between the candidate families change the answer? (graph only)."""
    from repoatlas import architecture_map as am

    intents = {sq.get("intent") for sq in sqs} or {"locate"}
    if intents & {"flow", "dataflow"}:
        others = [n for sq in sqs for mid in sq.get("mentions") or [] for n in links.get(mid, {}).get("nodes", [])
                  if n not in close]
        if others:
            connects = [c for c in close if any(_reaches(g, c, o) or _reaches(g, o, c) for o in others)]
            if len(connects) == 1:
                return {"same": False, "pick": connects[0],
                        "why": f"only {g.label(connects[0])} has a call path to the other mentions"}
            if not connects:
                return {"same": True, "why": "no candidate has a call path to the other mentions"}
    if "tests" in intents:
        tv = am.tests_view(g)
        sets = [frozenset(tv["covered"].get(f"{g.label(c)} ({am._loc(g, c)})", ())) for c in close]
        if len(set(sets)) == 1:
            return {"same": True, "why": "every candidate is reached by the same tests"}
    if "callers" in intents:
        sets = [frozenset(u for u, _ in g.in_edges(c, {"calls"})) for c in close]
        if len(set(sets)) == 1:
            return {"same": True, "why": "every candidate has the same callers"}
    files = {g.file(c) for c in close}
    if len(files) == 1:
        return {"same": True, "why": "every candidate is in the same file"}
    return {"same": False, "why": "candidates are in different files: " + ", ".join(sorted(f or "?" for f in files))}


def _reaches(g, a: str, b: str, depth: int = 6) -> bool:
    frontier, seen = {a}, {a}
    for _ in range(depth):
        nxt = set()
        for n in frontier:
            for v, _ in g.out_edges(n, {"calls"}):
                if v == b:
                    return True
                if v not in seen:
                    seen.add(v)
                    nxt.add(v)
        frontier = nxt
        if not frontier:
            break
    return False


# -- versions and references ---------------------------------------------------------------------

VERSION_RX = re.compile(
    r"(?P<semver>(?<![\w.])v?\d+\.\d+(?:\.\d+){0,2}(?:[-.]?(?:rc|beta|alpha|a|b|dev|post)\.?\d*)?(?![\w.]))"
    r"|(?P<sha>(?<![\w/.-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?![\w-]))"
    r"|(?P<pr>(?:(?<![\w&])#|\bPR\s?#?|\bpull/|\bMR\s?!?|(?<![\w])!)(?P<num>\d{1,7})\b)"
    r"|(?P<issue>\bissues/(?P<inum>\d{1,7})\b)",
    re.I)
VERSION_CUE = re.compile(r"(?:\b(?:v|version|versiyon\w*|surum\w*|release|tag|etiket\w*|python|node|java|go)\s*$)",
                         re.I)
VERSION_CUE_AFTER = re.compile(r"^['’]?\w*\s*(?:surum|versiyon|version|release|tag)", re.I)
RELATIVE_RX = re.compile(
    r"\b(eski|onceki|gecen|ilk|old|older|previous|prior|legacy|original|former)\b(?:\s+\w+){0,2}?\s+"
    r"(surum\w*|versiyon\w*|version\w*|release\w*|tag\w*|commit\w*|dal\w*|branch\w*|hali\w*)", re.I)
URL_RX = re.compile(r"https?://[^\s<>\"'`)]+")


def version_tokens(message: str) -> list[dict]:
    """Version-like tokens of a message: ``[{text, start, end, kind, strong}]``.

    A bare ``2.31`` counts as *strong* (must be carried by a reference) only
    with a ``v`` prefix, three components, or a version word next to it.
    """
    folded = tn.fold_tr(message)
    out = []
    for m in VERSION_RX.finditer(message):
        kind = next(k for k in ("semver", "sha", "pr", "issue") if m.group(k))
        s, e = m.span(kind)
        text = message[s:e]
        strong = True
        if kind == "semver":
            strong = (text.lower().startswith("v") or text.count(".") >= 2
                      or bool(VERSION_CUE.search(folded[:s].rstrip())) or bool(VERSION_CUE_AFTER.search(folded[e:])))
        out.append({"text": text, "start": s, "end": e, "kind": kind, "strong": strong})
    return out


def _carried(tok: str, refs: list[dict]) -> bool:
    t = tn.ground_key(tok)
    num = re.sub(r"\D", "", tok) if re.match(r"^(#|pr|pull/|mr|!|issues/)", tok, re.I) else None
    for r in refs:
        v = r.get("version") or {}
        hay = " ".join(tn.ground_key(x) for x in (v.get("spec") or "", v.get("evidence") or "", r.get("locator") or "",
                                                  r.get("text") or ""))
        if t in hay or (num and re.search(rf"(?<!\d){num}(?!\d)", hay)):
            return True
        if t.lstrip("v") and t.lstrip("v") == tn.ground_key(v.get("spec") or "").lstrip("v"):
            return True
    return False


def _git_tags(repo) -> list[str]:
    if repo is None:
        return []
    from repoatlas.snapshot import git

    out = git(Path(repo), "tag", "--list", "--sort=-creatordate")
    return [t.strip() for t in (out or "").splitlines() if t.strip()]


def check_references(plan: dict, repo=None, graph=None) -> tuple[list[dict], list[dict], list[dict]]:
    """Offline checks of the plan's references: ``(ref_checks, problems, clarifications)``."""
    checks, problems, clar = [], [], []
    tags: list[str] | None = None
    ix = _index(graph) if graph is not None else None
    for i, r in enumerate(plan.get("references") or []):
        v = r.get("version") or {}
        rc = {"reference": r.get("id"), "text": r.get("text"), "kind": r.get("kind"),
              "version_source": v.get("source", "unspecified")}
        spec = v.get("spec")
        local = not r.get("locator") and r.get("kind") in ("git_repo", "commit", "file", "symbol", "local_path")
        if spec:
            rc["version_used"] = spec
        if v.get("source") == "user_relative" and not spec:
            rc["status"] = "unresolved"
            rc["next_step"] = "ask which version (tag, commit or date) the user means"
            tags = _git_tags(repo) if tags is None else tags
            clar.append(_clarification(plan, f"c-{r.get('id')}", r.get("id"), "version", r.get("text") or "",
                                       [{"value": t, "label": t, "evidence": "git tag"} for t in tags[:4]],
                                       "the user named a relative version", _blocks(plan, r.get("id")),
                                       tags=tags[:4]))
        elif spec and local and r.get("kind") in ("git_repo", "local_path") and repo is not None:
            tags = _git_tags(repo) if tags is None else tags
            exact = [t for t in tags if t == spec]
            partial = [t for t in tags if t.startswith(spec) and t[len(spec):len(spec) + 1] in (".", "-")]
            if exact:
                rc["status"], rc["evidence"] = "resolved", f"local git tag {spec}"
            elif len(partial) > 1:
                rc["status"] = "ambiguous"
                rc["next_step"] = "choose one of the matching tags"
                clar.append(_clarification(plan, f"c-{r.get('id')}", r.get("id"), "version", r.get("text") or "",
                                           [{"value": t, "label": t, "evidence": "git tag"} for t in partial[:4]],
                                           f"'{spec}' matches {len(partial)} tags", _blocks(plan, r.get("id")),
                                           tags=partial[:4]))
            elif len(partial) == 1:
                rc["status"], rc["version_used"] = "resolved", partial[0]
                rc["evidence"] = f"the only local tag starting with {spec}"
            else:
                rc["status"] = "pending_research"
                rc["next_step"] = f"`repoatlas resolve \"{r.get('text')}\"` (no local tag named {spec})"
        elif r.get("kind") in ("file", "local_path", "symbol") and not spec and ix is not None:
            loc = r.get("locator") or r.get("text") or ""
            hit = _match_string(graph, ix, loc) if loc else []
            rc["status"] = "resolved" if hit else "unresolved"
            rc["version_used"] = "working tree"
            rc["evidence" if hit else "next_step"] = (f"found locally: {_at(graph, hit[0][0])}" if hit else
                                                      "give the path as it appears in the repository")
        else:
            rc["status"] = "pending_research"
            rc["next_step"] = (f"`repoatlas resolve \"{r.get('text')}\"`" + ("" if spec else
                               " - no version was given; research pins the default branch and says so"))
            if not spec and r.get("purpose") in ("compare", "behaviour_source"):
                problems.append(_problem(f"/references/{i}/version", "relative_version",
                                         "no version: research would use the default branch",
                                         "ask which version the user means, or record version.source"))
        checks.append(rc)
    return checks, problems, clar


def _blocks(plan: dict, item_id: str | None) -> list[str]:
    out = []
    for sq in plan.get("sub_questions") or []:
        if item_id in (sq.get("mentions") or []) or item_id in (sq.get("references") or []) \
                or item_id == sq.get("id") or item_id in ((sq.get("done_when") or {}).get("subjects") or []):
            out.append(sq.get("id"))
    return out


# -- clarifications -------------------------------------------------------------------------------

TEMPLATES = {
    "entity": ("Which one do you mean by '{x}'?", "'{x}' ile hangisini kastediyorsunuz?"),
    "did_you_mean": ("'{x}' was not found. Did you mean one of these?",
                     "'{x}' bulunamadı. Bunlardan birini mi kastettiniz?"),
    "version": ("Which version do you mean by '{x}'?", "'{x}' için hangi sürümü kastediyorsunuz?"),
    "deixis": ("What does '{x}' refer to?", "'{x}' ile neyi kastediyorsunuz?"),
}
TAGS_SUFFIX = (" (tags found: {tags})", " (bulunan etiketler: {tags})")


def _clarification(plan: dict, cid: str, about: str | None, kind: str, x: str, options: list[dict], why: str,
                   blocks: list[str], tags: list[str] | None = None) -> dict:
    en, tr = TEMPLATES[kind]
    en, tr = en.format(x=x), tr.format(x=x)
    if tags:
        en += TAGS_SUFFIX[0].format(tags=", ".join(tags))
        tr += TAGS_SUFFIX[1].format(tags=", ".join(tags))
    user = tr if plan.get("language") in ("tr", "mixed") else en
    opts = list(options)
    if kind in ("entity", "did_you_mean") and len(opts) > 1:
        opts.append({"value": "__all__", "label": "all of them / hepsi", "evidence": "every candidate above"})
    opts.append({"value": "__other__", "label": "something else / başka bir şey", "evidence": "free text"})
    return {"id": cid, "about": about, "kind": kind, "question_user_lang": user, "question_en": en,
            "options": opts[:5 if kind != "entity" else 6], "why": why, "blocks": blocks}


def _entity_clarifications(plan: dict, graph, links: list[dict]) -> list[dict]:
    out = []
    mentions = {m["id"]: m for m in plan.get("mentions") or []}
    for lk in links:
        m = mentions.get(lk["mention"], {})
        if lk["status"] == "ambiguous":
            opts = [{"value": n, "label": f"{graph.label(n)} ({_at(graph, n)})",
                     "evidence": next((c["matches"][0]["type"] for c in [lk.get("best")] + lk["alternatives"]
                                       if c and c["node"] == n and c["matches"]), "graph")}
                    for n in lk["nodes"][:4]]
            out.append(_clarification(plan, f"c-{lk['mention']}", lk["mention"], "entity", lk["text"], opts,
                                      lk.get("probe") or "several candidates score alike",
                                      _blocks(plan, lk["mention"])))
        elif lk["status"] == "unlinked" and m.get("required", True) and not _is_concept(m) and lk.get("near_misses"):
            opts = [{"value": nm["node"], "label": f"{nm['label']} ({nm['at']})",
                     "evidence": f"similar name ({nm['similarity']})"} for nm in lk["near_misses"][:4]]
            out.append(_clarification(plan, f"c-{lk['mention']}", lk["mention"], "did_you_mean", lk["text"], opts,
                                      "no exact match; similar names exist", _blocks(plan, lk["mention"])))
    return out


def _deixis_clarifications(plan: dict, graph, links: list[dict]) -> list[dict]:
    if plan.get("conversation_context"):
        return []
    out = []
    linked = [n for lk in links if lk["status"] in ("linked", "weak") for n in lk["nodes"][:1]]
    for sq in plan.get("sub_questions") or []:
        if sq.get("subject_from") or sq.get("mentions") or sq.get("references"):
            continue
        words = set(re.findall(r"[a-z]+", tn.fold_tr(sq.get("text_user_lang") or sq.get("text") or "")))
        ana = sorted(words & (EN_ANAPHORS | TR_ANAPHORS) - {"o", "bu", "su", "that", "this"} or
                     words & {"bunu", "onu", "sunu", "it", "them"})
        if not ana:
            continue
        opts = [{"value": n, "label": f"{graph.label(n)} ({_at(graph, n)})", "evidence": "linked elsewhere in the plan"}
                for n in dict.fromkeys(linked)][:3]
        out.append(_clarification(plan, f"c-{sq['id']}", sq["id"], "deixis", ana[0], opts,
                                  "the sub-question points at something the message does not name", [sq["id"]]))
    return out


# -- check ------------------------------------------------------------------------------------------

def _topo(sqs: list[dict]) -> tuple[list[str], list[str]]:
    deps = {sq["id"]: set(sq.get("depends_on") or []) | ({sq["subject_from"]} if sq.get("subject_from") else set())
            for sq in sqs}
    ids = list(deps)
    indeg = {k: len(v & deps.keys()) for k, v in deps.items()}
    ready = [k for k in ids if indeg[k] == 0]
    order = []
    while ready:
        k = ready.pop(0)
        order.append(k)
        for j in ids:
            if k in deps[j]:
                indeg[j] -= 1
                if indeg[j] == 0:
                    ready.append(j)
    return order, [k for k in ids if k not in order]


def _integrity(plan: dict) -> tuple[list[dict], list[dict], list[str]]:
    errors, warnings = [], []
    ids: dict[str, str] = {}
    for kind in ("sub_questions", "mentions", "references", "interpretations"):
        for i, it in enumerate(plan.get(kind) or []):
            iid = it.get("id")
            if iid in ids:
                errors.append(_problem(f"/{kind}/{i}/id", "duplicate_id", f"id {iid} is used twice",
                                       "give every item its own id"))
            ids[iid] = kind
    sqs = plan.get("sub_questions") or []
    for i, sq in enumerate(sqs):
        for key, want in (("mentions", "mentions"), ("references", "references"), ("depends_on", "sub_questions")):
            for j, ref in enumerate(sq.get(key) or []):
                if ids.get(ref) != want:
                    errors.append(_problem(f"/sub_questions/{i}/{key}/{j}", "dangling_id", f"{ref} is not a known "
                                           f"{want[:-1].replace('_', '-')} id", f"define {ref} or remove it"))
        if sq.get("subject_from") and ids.get(sq["subject_from"]) != "sub_questions":
            errors.append(_problem(f"/sub_questions/{i}/subject_from", "dangling_id",
                                   f"{sq['subject_from']} is not a sub-question id",
                                   "point at an earlier sub-question"))
        dw = sq.get("done_when") or {}
        own = set(sq.get("mentions") or []) | set(sq.get("references") or [])
        for j, subj in enumerate(dw.get("subjects") or []):
            if ids.get(subj) not in ("mentions", "references"):
                errors.append(_problem(f"/sub_questions/{i}/done_when/subjects/{j}", "dangling_id",
                                       f"{subj} is not a mention or reference id", "use ids from mentions/references"))
            elif subj not in own:
                warnings.append(_problem(f"/sub_questions/{i}/done_when/subjects/{j}", "subject_scope",
                                         f"{subj} is not listed in the sub-question's own mentions/references",
                                         f"add {subj} to sub_questions[{i}].mentions or .references"))
        if dw.get("kind") and sq.get("intent") in INTENT_DONE_OK and dw["kind"] not in INTENT_DONE_OK[sq["intent"]]:
            warnings.append(_problem(f"/sub_questions/{i}/done_when/kind", "intent_done_when_mismatch",
                                     f"intent {sq['intent']} is not usually judged by {dw['kind']}",
                                     f"typical: {DEFAULT_DONE[sq['intent']][0]}"))
    order, cyclic = _topo(sqs)
    if cyclic:
        errors.append(_problem("/sub_questions", "cycle", f"depends_on/subject_from form a cycle through {cyclic}",
                               "a sub-question may only depend on earlier ones"))
    for key, lim in (("sub_questions", LIMITS["sub_questions"]), ("mentions", LIMITS["mentions"]),
                     ("references", LIMITS["references"])):
        if len(plan.get(key) or []) > lim:
            errors.append(_problem(f"/{key}", "limit", f"at most {lim} {key.replace('_', '-')} allowed",
                                   "merge or drop the least important ones"))
    return errors, warnings, order


def _grounding(plan: dict) -> tuple[list[dict], list[dict]]:
    errors, warnings = [], []
    msg = plan.get("user_message") or ""
    low = tn.ground_key(msg)
    ctx = tn.ground_key(" \n ".join(plan.get("conversation_context") or []))
    for kind in ("mentions", "references"):
        for i, it in enumerate(plan.get(kind) or []):
            conv = it.get("source") == "conversation" or it.get("locator_source") == "conversation"
            text = it.get("text") or ""
            where = ctx if conv else low
            if tn.ground_key(text) not in where:
                errors.append(_problem(f"/{kind}/{i}/text", "ungrounded_text",
                                       f"{text!r} does not occur verbatim in the "
                                       f"{'conversation context' if conv else 'user message'}",
                                       "copy the user's exact words; put your own guesses into candidates, gloss_en "
                                       "or assumptions"))
                continue
            sp = it.get("span")
            if sp and not conv and (not (0 <= sp[0] <= sp[1] <= len(msg))
                                    or tn.ground_key(msg[sp[0]:sp[1]]) != tn.ground_key(text)):
                warnings.append(_problem(f"/{kind}/{i}/span", "span_mismatch",
                                         "span does not select the text; the span is ignored",
                                         "fix the offsets or omit span"))
    return errors, warnings


def _versions(plan: dict) -> tuple[list[dict], list[dict], list[dict]]:
    errors, warnings, clar = [], [], []
    msg = plan.get("user_message") or ""
    refs = plan.get("references") or []
    for tok in version_tokens(msg):
        if _carried(tok["text"], refs):
            continue
        p = _problem("/references", "version_dropped",
                     f"the user wrote {tok['text']!r} but no reference carries it",
                     "add it to references[].version.spec (or to locator for a PR/issue/commit); never drop a "
                     "version the user named")
        (errors if tok["strong"] else warnings).append(p)
    rel = RELATIVE_RX.search(tn.fold_tr(msg))
    if rel and not any((r.get("version") or {}).get("source") == "user_relative" for r in refs):
        words = msg[rel.start():rel.end()]
        warnings.append(_problem("/references", "relative_version",
                                 f"the user named a relative version ({words!r})",
                                 "record it as a reference with version.source=user_relative, or ask which version"))
        first = (plan.get("sub_questions") or [{}])[0].get("id")
        clar.append(_clarification(plan, "c-version", first, "version", words, [], "relative version word",
                                   [sq.get("id") for sq in plan.get("sub_questions") or []]))
    return errors, warnings, clar


def check(plan: dict, graph, repo=None, lexicon=None, *, source: str = "host") -> dict:
    """Validate and ground a plan. Pure apart from reading source lines and local git tags."""
    result: dict = {"schema": CHECK_SCHEMA_ID, "plan_hash": plan_hash(plan) if isinstance(plan, dict) else None,
                    "source": source, "status": "invalid", "errors": [], "warnings": [], "thresholds": THRESHOLDS,
                    "language_detected": None, "intents_detected": [], "intent_divergence": [], "topo_order": [],
                    "links": [], "references": [], "clarifications": [], "unknowns": []}
    if not isinstance(plan, dict):
        result["errors"] = [_problem("/", "schema", "a plan is a JSON object", "see `repoatlas plan schema`")]
        return result
    probs = validate(plan)
    if probs:
        result["errors"] = probs
        return result
    lex = _lexicon_for(graph, lexicon) if graph is not None else lexicon
    errors, warnings, order = _integrity(plan)
    e2, w2 = _grounding(plan)
    e3, w3, clar_v = _versions(plan)
    errors += e2 + e3
    warnings += w2 + w3
    result["topo_order"] = order
    msg = plan["user_message"]
    lang = tn.detect_language(msg)
    result["language_detected"] = lang
    if {lang, plan["language"]} == {"tr", "en"}:
        warnings.append(_problem("/language", "language", f"the message looks {lang}, the plan says "
                                 f"{plan['language']}", "answer in the user's language"))
    rules = intents_for(msg, lex)
    result["intents_detected"] = rules
    for sq in plan["sub_questions"]:
        if rules and sq["intent"] not in rules:
            result["intent_divergence"].append({"sub_question": sq["id"], "plan": sq["intent"], "rules": rules})
    if result["intent_divergence"]:
        warnings.append(_problem("/sub_questions", "intent_divergence",
                                 "the plan's intents differ from the rule-based reading "
                                 f"({[d['sub_question'] + ':' + d['plan'] for d in result['intent_divergence']]} vs "
                                 f"{rules}); the plan is kept", "check that each sub-question's intent is what the "
                                 "user asked"))
    for i, m in enumerate(plan.get("mentions") or []):
        if len(m.get("candidates") or []) > LIMITS["candidates"]:
            errors.append(_problem(f"/mentions/{i}/candidates", "limit", "at most 5 candidates", "keep the best"))
    ref_checks, ref_probs, clar_r = check_references(plan, repo, graph)
    warnings += ref_probs
    result["references"] = ref_checks
    clar = clar_v + clar_r
    if graph is not None and not errors:
        links = link_mentions(plan, graph, lex, repo=repo)
        result["links"] = links
        clar = _entity_clarifications(plan, graph, links) + _deixis_clarifications(plan, graph, links) + clar
        mentions = {m["id"]: m for m in plan.get("mentions") or []}
        for lk in links:
            m = mentions[lk["mention"]]
            if lk["status"] == "unlinked" and m.get("required", True) and not lk.get("near_misses"):
                result["unknowns"].append({
                    "about": lk["mention"], "question": f"what is '{lk['text']}' in this repository?",
                    "why": f"no graph node, file or lexicon entry matches '{lk['text']}'",
                    "next_step": "give the identifier or path as written in the code, or browse with "
                                 "`repoatlas map` / `repoatlas query`"})
    for ans in plan.get("answers") or []:
        if not any(c["id"] == ans.get("clarification_id") for c in clar) and not ans.get("clarification_id", "") \
                .startswith("c-"):
            warnings.append(_problem("/answers", "answer_unmatched",
                                     f"answer {ans.get('clarification_id')!r} matches no clarification",
                                     "use the clarification ids returned by plan check"))
    answered = {a.get("clarification_id") for a in plan.get("answers") or []}
    clar = [c for c in clar if c["id"] not in answered]
    clar.sort(key=lambda c: -len(c["blocks"]))
    if len(clar) > THRESHOLDS["max_clarifications"]:
        warnings.append(_problem("/", "too_many_clarifications",
                                 f"{len(clar)} clarifications found; only the {THRESHOLDS['max_clarifications']} "
                                 "that block the most sub-questions are asked", "split the question"))
    result["clarifications"] = clar[:THRESHOLDS["max_clarifications"]]
    result["errors"], result["warnings"] = errors, warnings
    result["status"] = "invalid" if errors else "needs_clarification" if result["clarifications"] else "ready"
    return result


def compact_check(res: dict) -> dict:
    """The part of a check result an agent needs (for analyze output)."""
    links = []
    for lk in res.get("links") or []:
        best = lk.get("best") or {}
        item = {"mention": lk["mention"], "text": lk["text"], "status": lk["status"]}
        if best and lk["status"] != "unlinked":
            item.update({"at": best.get("at"), "label": best.get("label"), "score": best.get("score"),
                         "tier": lk.get("tier")})
        if lk.get("family_merged"):
            item["merged"] = len(lk["family_merged"])
        if lk.get("rejected_candidates"):
            item["rejected"] = [r["candidate"] for r in lk["rejected_candidates"]]
        links.append(item)
    return {"status": res.get("status"), "errors": res.get("errors") or [],
            "warnings": [{"code": w["code"], "msg": w["msg"]} for w in res.get("warnings") or []],
            "links": links, "references": res.get("references") or [],
            "clarifications": res.get("clarifications") or [],
            "intent_divergence": res.get("intent_divergence") or []}


# -- storage ----------------------------------------------------------------------------------------

def store_plan(store, plan: dict, check_result: dict, source: str, *, parent_id: str | None = None,
               analysis_id: str | None = None, snapshot_id: str | None = None) -> str:
    """Record a plan and its check (immutable; revisions link through ``parent_id``). Returns its id."""
    from repoatlas.store import new_id, now

    if source not in ("host", "fallback", "revised"):
        raise ValueError(f"source must be host/fallback/revised, not {source!r}")
    pid = new_id("qpl")
    store.insert("question_plans", {
        "id": pid, "parent_id": parent_id, "analysis_id": analysis_id, "source": source,
        "schema_id": SCHEMA_ID, "user_message": str(plan.get("user_message") or ""),
        "plan": plan, "plan_hash": plan_hash(plan), "check_result": check_result,
        "status": check_result.get("status") or "invalid", "snapshot_id": snapshot_id, "created_at": now(),
    })
    return pid


def get_plan(store, plan_id: str) -> dict | None:
    return store.get("question_plans", plan_id)


# -- draft (rules) ----------------------------------------------------------------------------------

_TOKEN_RX = re.compile(r"`[^`]+`|\"[^\"]{2,}\"|“[^”]{2,}”|[\w][\w.'’/-]*[\w]|\w", re.UNICODE)
_UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_GENERIC_EN = frozenset("""
test tests testing function functions method methods class classes code file files module modules line lines call
calls called path flow happen happens happened need needs needed thing things something work works way ways use
uses used using make makes made get gets got go goes going do does done put puts take takes come comes give gives
turn turns keep keeps let lets affected affect affects change changes changed decide decides control controls
set sets exist exists current default new old please show tell explain find return returns
""".split())
_GENERIC_TR_PREFIX = ("fonksiyon", "dosya", "modul", "sinif", "metot", "metod", "satir", "test", "islev", "kodu",
                      "kodda", "kodun", "yeri", "yerde", "sey", "sekil", "hangi", "nasil")
_PERSISTENCE_WORDS = ("database", "databases", "db", "storage", "store", "persist", "persistence", "disk", "sql",
                      "veritaban", "kalici", "depo", "sakla")


def _code_kind(tok: str) -> str | None:
    """Mention kind for a code-like token, None for a plain word."""
    if tok.startswith("`") and tok.endswith("`"):
        inner = tok.strip("`")
        return _code_kind(inner) or "symbol"
    if tok[:1] in "\"“":
        return "error_message" if " " in tok else "symbol"
    base = tok.rstrip("()")
    suffix = PurePosixPath(base).suffix[1:].lower()
    if "/" in base or (suffix in _CODE_EXTS and "." in base):
        return "file"
    if _UPPER_SNAKE.match(base):
        return "env_var"
    if tok.endswith("()") or "." in base.strip(".") or "_" in base.strip("_") or base.startswith("_") \
            or re.search(r"[a-z0-9][A-Z]", base):
        return "symbol"
    return None


def _role_of(tok: str, lex) -> str | None:
    base, suf = tn.split_apostrophe(tok)
    if suf:
        return tn.suffix_role(suf)
    w = tn.fold_tr(base).lower()
    for hit in lex.seed([w]):
        key = hit["key"].split()[-1]
        rest = w[len(key):]
        if rest:
            return tn.suffix_role(rest)
    for stem in tn.tr_stem_candidates(w)[1:]:
        if lex.has(stem):
            return tn.suffix_role(w[len(stem):])
    return None


def _en_roles(clause: str, offset: int) -> dict[tuple[int, int], str]:
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?=[?.!,;]|\band\b|$)", clause, re.I)
    if not m:
        return {}
    return {(offset + m.start(1), offset + m.end(1)): "source", (offset + m.start(2), offset + m.end(2)): "target"}


def _tokens(message: str) -> list[dict]:
    out = []
    for m in _TOKEN_RX.finditer(message):
        tok = m.group(0)
        s, e = m.start(), m.end()
        while tok and tok[-1] in ".,;:!?'’":
            tok, e = tok[:-1], e - 1
        if tok:
            out.append({"text": tok, "start": s, "end": e})
    return out


def _gloss(lex, word: str) -> str | None:
    hits = lex.seed([tn.fold_tr(word).lower()])
    targets = [t for h in hits for t in h["targets"]]
    return targets[0] if targets else None


def draft(question: str, graph, lexicon=None) -> dict:
    """A plan built from rules (no LLM); every element is tagged ``derived_by``."""
    lex = _lexicon_for(graph, lexicon) if graph is not None else lexicon
    lang = tn.detect_language(question)
    clauses = segment(question)[:LIMITS["sub_questions"]]
    toks = _tokens(question)
    ref_list, ref_spans = _draft_references(question)
    mentions: list[dict] = []
    per_clause: dict[int, list[str]] = defaultdict(list)
    anaphoric: dict[int, bool] = {}

    def clause_of(pos: int) -> int:
        for i, c in enumerate(clauses):
            if c["start"] <= pos < max(c["end"], c["start"] + 1):
                return i
        return len(clauses) - 1

    roles: dict[tuple[int, int], str] = {}
    for c in clauses:
        roles.update(_en_roles(c["text"], c["start"]))

    def role_at(tok: dict) -> str | None:
        for (a, b), r in roles.items():
            if a <= tok["start"] and tok["end"] <= b:
                return r
        return _role_of(tok["text"], lex) if lex is not None and lang in ("tr", "mixed") else None

    pending: list[dict] = []
    for tok in toks:
        if any(a <= tok["start"] < b for a, b in ref_spans):
            continue
        text = tok["text"]
        base, _ = tn.split_apostrophe(text)
        folded = tn.fold_tr(base).lower()
        ci = clause_of(tok["start"])
        if folded in EN_ANAPHORS or folded in TR_ANAPHORS:
            anaphoric[ci] = True
            continue
        kind = _code_kind(base)
        if kind:
            pending.append({"tok": tok, "kind": kind, "required": True, "clause": ci})
            continue
        if (folded in tn.TR_STOPWORDS or folded in tn.EN_STOPWORDS or folded in tn.TR_QUESTION_WORDS
                or folded in _GENERIC_EN or folded.startswith(_GENERIC_TR_PREFIX) or folded.isdigit()
                or (len(folded) < 3 and folded not in tn.SHORT_TECH)):
            continue
        pending.append({"tok": tok, "kind": "domain_concept", "required": False, "clause": ci})
    # adjacent plain words that together name one thing ("cache entry" -> cache_entry)
    merged: list[dict] = []
    i = 0
    while i < len(pending):
        cur = pending[i]
        nxt = pending[i + 1] if i + 1 < len(pending) else None
        if (graph is not None and nxt and cur["kind"] == nxt["kind"] == "domain_concept"
                and cur["clause"] == nxt["clause"]
                and not question[cur["tok"]["end"]:nxt["tok"]["start"]].strip(" -")):
            phrase = question[cur["tok"]["start"]:nxt["tok"]["end"]]
            lk = link_mention({"id": "m0", "text": phrase, "kind": "domain_concept"}, graph, lex, with_hash=False)
            best_label = _bare((lk.get("best") or {}).get("label", ""))
            if lk["status"] == "linked" and lk.get("tier") in NAME_TIERS | {"identifier_parts"} \
                    and len(tn.split_identifier(best_label)) >= 2:
                single = link_mention({"id": "m0", "text": cur["tok"]["text"], "kind": "domain_concept"}, graph, lex,
                                      with_hash=False)
                if len(single.get("nodes") or []) != 1 or single["nodes"] != lk["nodes"]:
                    tok = {"text": phrase, "start": cur["tok"]["start"], "end": nxt["tok"]["end"]}
                    merged.append({**cur, "tok": tok})
                    i += 2
                    continue
        merged.append(cur)
        i += 1
    for p in merged:
        tok = p["tok"]
        m = {"id": f"m{len(mentions) + 1}", "text": tok["text"], "span": [tok["start"], tok["end"]],
             "kind": p["kind"], "required": p["required"], "derived_by": f"{DRAFT_RULES}:"
             + ("code_token" if p["required"] else "content_word")}
        if graph is not None and not p["required"]:
            lk = link_mention(m, graph, lex, with_hash=False)
            if lk["status"] == "unlinked":
                continue
        role = role_at(tok)
        if role:
            m["role"] = role
        if lex is not None and lang in ("tr", "mixed"):
            g_en = _gloss(lex, tn.split_apostrophe(tok["text"])[0])
            if g_en:
                m["gloss_en"] = g_en
        if len(mentions) >= LIMITS["mentions"]:
            break
        mentions.append(m)
        per_clause[p["clause"]].append(m["id"])
    references = []
    for r in ref_list[:LIMITS["references"]]:
        r["id"] = f"r{len(references) + 1}"
        references.append(r)
    ref_clause = defaultdict(list)
    for r in references:
        ref_clause[clause_of(r["span"][0])].append(r["id"])
    sqs = []
    for ci, c in enumerate(clauses):
        cues = clause_cues(c["text"], lex)
        intent = cues[0]["intent"] if cues else "locate"
        mids = per_clause.get(ci, [])
        rids = ref_clause.get(ci, [])
        kind, min_status, detail = DEFAULT_DONE[intent]
        subjects = list(mids)
        if intent == "flow":
            ms = {m["id"]: m for m in mentions}
            src = [x for x in mids if ms[x].get("role") == "source"]
            tgt = [x for x in mids if ms[x].get("role") == "target"]
            subjects = src[:1] + [x for x in mids if x not in src[:1] + tgt[-1:]] + tgt[-1:]
        if intent == "compare_reference":
            subjects += rids
        sq = {"id": f"q{ci + 1}", "text": c["text"], "text_user_lang": c["text"], "intent": intent,
              "mentions": mids, "references": rids,
              "done_when": {"kind": kind, "subjects": subjects, "min_status": min_status, "detail": detail},
              "derived_by": f"{DRAFT_RULES}:" + ("+".join(f"{x['intent']}<-{x['cue']}" for x in cues[:3])
                                                 or "default_locate")}
        secondary = [x["intent"] for x in cues[1:]]
        if secondary:
            sq["secondary_intents"] = secondary
        if ci > 0 and (anaphoric.get(ci) or c.get("conditional")):
            sq["subject_from" if anaphoric.get(ci) else "depends_on"] = (
                f"q{ci}" if anaphoric.get(ci) else [f"q{ci}"])
            if anaphoric.get(ci) and c.get("conditional"):
                sq["depends_on"] = [f"q{ci}"]
        if not sq["references"]:
            del sq["references"]
        sqs.append(sq)
    plan = {"schema": SCHEMA_ID, "user_message": question, "language": lang if lang != "other" else "other",
            "restated_goal": _goal_en(sqs, mentions), "restated_goal_user_lang": _goal_user(sqs, mentions, lang),
            "sub_questions": sqs, "mentions": mentions, "references": references,
            "assumptions": ["drafted by rules without an LLM; intents come from cue words, mentions from words that "
                            "match the code"],
            "on_ambiguity": "answer_all", "host": "cli", "derived_by": DRAFT_RULES}
    if not references:
        del plan["references"]
    return plan


def _goal_en(sqs: list[dict], mentions: list[dict]) -> str:
    parts = [f"{sq['id']} [{sq['intent']}] {sq['text']}" for sq in sqs]
    gl = [f"{m['text']}={m['gloss_en']}" for m in mentions if m.get("gloss_en")]
    return "Rule-based reading: " + " | ".join(parts) + (f" (glosses: {', '.join(gl)})" if gl else "")


def _goal_user(sqs: list[dict], mentions: list[dict], lang: str) -> str:
    if lang not in ("tr", "mixed"):
        return "Understood (rules): " + " | ".join(f"{sq['id']} [{sq['intent']}] {sq['text']}" for sq in sqs)
    gl = [f"{m['text']}={m['gloss_en']}" for m in mentions if m.get("gloss_en")]
    return ("Anladığım (kurallarla): "
            + " | ".join(f"{sq['id']} [{INTENT_NAMES_TR[sq['intent']]}] {sq['text']}" for sq in sqs)
            + (f" (karşılıklar: {', '.join(gl)})" if gl else ""))


def _draft_references(question: str) -> tuple[list[dict], list[tuple[int, int]]]:
    """References with the versions the user wrote; every strong version token is carried."""
    refs: list[dict] = []
    spans: list[tuple[int, int]] = []
    derived = f"{DRAFT_RULES}:version_token"
    for m in URL_RX.finditer(question):
        url = m.group(0).rstrip(".,;:!?)")
        s, e = m.start(), m.start() + len(url)
        ver = re.search(r"/(?:tree|blob|releases/tag|commit|compare)/([^/#?]+)", url)
        kind = ("pull_request" if re.search(r"/pull/\d+|/merge_requests/\d+", url) else
                "issue" if re.search(r"/issues/\d+", url) else
                "paper" if re.search(r"arxiv\.org|doi\.org", url) else
                "git_repo" if re.search(r"github\.com|gitlab\.com|bitbucket\.org", url) else "doc_url")
        v = ({"spec": ver.group(1), "source": "user_explicit", "evidence": ver.group(1)} if ver
             else {"source": "unspecified"})
        refs.append({"text": url, "span": [s, e], "kind": kind, "locator": url, "locator_source": "user_message",
                     "version": v, "purpose": "context", "derived_by": f"{DRAFT_RULES}:url"})
        spans.append((s, e))
    for tok in version_tokens(question):
        if any(a <= tok["start"] < b for a, b in spans):
            continue
        s, e = tok["start"], tok["end"]
        kind = {"sha": "commit", "pr": "pull_request", "issue": "issue"}.get(tok["kind"], "git_repo")
        text, locator = tok["text"], None
        before = question[:s]
        nm = re.search(r"([A-Za-z][\w.-]*)(==|@|\s+)$", before)
        if nm and tok["kind"] == "semver":
            name = nm.group(1)
            if not name.lower() in ("version", "v", "surum", "sürüm", "release", "tag") \
                    and tn.fold_tr(name).lower() not in tn.TR_STOPWORDS | tn.EN_STOPWORDS:
                s = s - len(nm.group(0))
                text = question[s:e]
                locator = name
                kind = "package" if nm.group(2) in ("==", "@") else "git_repo"
        if not tok["strong"] and not locator:
            continue
        refs.append({"text": text, "span": [s, e], "kind": kind,
                     **({"locator": locator, "locator_source": "user_message"} if locator else {}),
                     "version": {"spec": tok["text"], "source": "user_explicit", "evidence": tok["text"]},
                     "purpose": "context", "derived_by": derived})
        spans.append((s, e))
    folded = tn.fold_tr(question)
    for m in RELATIVE_RX.finditer(folded):
        s, e = m.start(), m.end()
        if any(a <= s < b for a, b in spans):
            continue
        refs.append({"text": question[s:e], "span": [s, e], "kind": "git_repo",
                     "version": {"source": "user_relative", "evidence": question[m.start(1):m.end(1)]},
                     "purpose": "context", "derived_by": f"{DRAFT_RULES}:relative_version"})
        spans.append((s, e))
    refs.sort(key=lambda r: r["span"][0])
    return refs, spans


# -- inputs for retrieval ---------------------------------------------------------------------------

def mention_nodes(link: dict) -> list[str]:
    """Nodes a link stands for (the merged families' best members included)."""
    nodes = list(link.get("nodes") or [])
    for n in link.get("family_merged") or []:
        if n not in nodes:
            nodes.append(n)
    return nodes


def is_persistence_word(text: str) -> bool:
    f = tn.fold_tr(text).lower()
    return any(w.startswith(_PERSISTENCE_WORDS) for w in re.findall(r"[a-z]+", f))


def retrieval_inputs(plan: dict, check_result: dict, sq: dict, lexicon=None, *,
                     subject_nodes: dict[str, str] | None = None) -> dict:
    """Query text, seeds and expansions for one sub-question (the retrieval contract).

    * ``seeds``: nodes of mentions linked at a *name* tier (the user spelled
      the name) plus the subjects carried over through ``subject_from``;
      rejected host candidates never appear.
    * ``expansions``: for words the repository does not use itself, their
      grounded seed-dictionary targets, repo-learned lexicon parts and the
      mention's ``gloss_en`` words.
    """
    links = {lk["mention"]: lk for lk in check_result.get("links") or []}
    mentions = {m["id"]: m for m in plan.get("mentions") or []}
    seeds: dict[str, str] = {}
    for mid in sq.get("mentions") or []:
        lk = links.get(mid)
        if not lk or lk["status"] != "linked":
            continue
        # Only a name the user (or the host, through a candidate) actually wrote
        # as code seeds retrieval; a plain word that happens to equal a label
        # ("query" -> .query()) is a concept and stays a search word.
        via_candidate = any(m_.get("via", "").startswith("candidate:")
                            for m_ in (lk.get("best") or {}).get("matches", [])[:1])
        named = lk.get("tier") in NAME_TIERS and (not _is_concept(mentions.get(mid, {})) or via_candidate)
        if named or lk.get("answered"):
            for n in mention_nodes(lk)[:5]:
                seeds.setdefault(n, f"plan mention {mid} '{lk['text']}' linked ({lk.get('tier') or 'answer'})")
    for n, why in (subject_nodes or {}).items():
        seeds.setdefault(n, why)
    expansions: dict[str, list[str]] = {}
    text = sq.get("text") or ""
    user_text = sq.get("text_user_lang") or ""
    words = []
    for t in tn.raw_tokens(text + " " + (user_text if user_text != text else "")):
        for w in re.findall(r"[a-z0-9_]+", tn.fold_tr(t).lower()):
            if len(w) >= 3 and w not in tn.TR_STOPWORDS and w not in tn.EN_STOPWORDS and w not in words:
                words.append(w)
    if lexicon is not None:
        for hit in lexicon.seed(words):
            src = " ".join(words[hit["start"]:hit["start"] + hit["n"]])
            if hit["targets"] and not all(lexicon.has(x) for x in src.split()):
                expansions.setdefault(src, [])
                expansions[src] += [t for t in hit["targets"] if t not in expansions[src]]
        for w in words:
            if lexicon.has(w) or any(w in k.split() for k in expansions):
                continue
            parts = [pr["part"] for pr in lexicon.associations(w)[:3] if pr["score"] >= 0.3]
            if parts:
                expansions[w] = parts
    for mid in sq.get("mentions") or []:
        m = mentions.get(mid) or {}
        gloss = [w for w in re.findall(r"[a-z0-9_]+", (m.get("gloss_en") or "").lower())
                 if w not in tn.EN_STOPWORDS and (lexicon is None or lexicon.has(w))]
        src = tn.fold_tr(tn.split_apostrophe(m.get("text") or "")[0]).lower()
        if gloss and (lexicon is None or not lexicon.has(src)):
            expansions.setdefault(src, [])
            expansions[src] += [g for g in gloss if g not in expansions[src]]
    return {"query": text if text.strip() else user_text, "seeds": seeds, "expansions": expansions}
