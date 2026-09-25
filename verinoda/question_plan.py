"""Question plans: what the user asked, checked against the code before any work.

Design: docs/DESIGN.md D1-D4 and D8. A plan (``verinoda.question_plan/1``,
schema in ``verinoda/schemas/question_plan.v1.json``) restates the user's
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
   step). A mention written as code (backticks, a path, snake_case,
   camelCase, dotted, ``name()``) is never replaced by a similar name: it
   needs a name tier; otherwise it is at most *weak* when the repository
   spells it somewhere outside import statements (:func:`name_site`), and
   *not_found* (with ``did_you_mean`` and the first unknown "no symbol named
   `x` in this repository; nearest: ...") when it spells it nowhere; its
   sub-question is ``unmet``. Before an ambiguous entity is asked about, a
   graph probe checks
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

import ast
import hashlib
import json
import math
import re
import sqlite3
import time
from collections import defaultdict
from functools import lru_cache
from importlib import resources
from pathlib import Path, PurePosixPath

from verinoda import textnorm as tn

SCHEMA_ID = "verinoda.question_plan/1"
CHECK_SCHEMA_ID = "verinoda.plan_check/1"
DRAFT_RULES = "question_plan.draft/1"

INTENTS = ("locate", "define", "flow", "callers", "dataflow", "config", "tests", "why", "history", "impact",
           "behaviour", "compare_reference", "performance", "architecture", "usage", "decide")
DONE_KINDS = ("location_verified", "path_found", "set_enumerated", "claim_exists", "decision_found",
              "proposition_checked", "reference_pinned", "comparison_done", "decision_brief")
# A sub-question Verinoda must not settle (docs/DESIGN.md D33): a choice between options is the human's.
# Its verdict is always this, never met.
HUMAN_DECISION = "human_decision_required"
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
    "decide": ("decision_brief", "strong_inference", "the forces from the code, what is absent, existing decisions "
               "and the questions only the human can answer; the choice stays with the human"),
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
    "decide": frozenset({"decision_brief"}),
}
INTENT_NAMES_TR = {
    "locate": "konum", "define": "tanım", "flow": "akış", "callers": "çağıranlar", "dataflow": "veri yolu",
    "config": "yapılandırma", "tests": "testler", "why": "gerekçe", "history": "geçmiş", "impact": "etki",
    "behaviour": "davranış", "compare_reference": "karşılaştırma", "performance": "performans",
    "architecture": "mimari", "usage": "kullanım", "decide": "karar",
}

# -- schema ---------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def schema() -> dict:
    """The packaged JSON Schema of ``verinoda.question_plan/1``."""
    raw = resources.files("verinoda").joinpath("schemas/question_plan.v1.json").read_text(encoding="utf-8")
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
                out.append(_problem(f"{at}/{k}", "schema", "unknown field", "remove it (see `verinoda plan schema`)"))
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
                                   "write the plan to a file under .verinoda/plans/ instead of the command line")]
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
    # "decide" (a choice the human makes) is set below: DECIDE_STRONG_EN + DECIDE_EN, with vetoes
}
# Weak cues count only when no other cue fires in the clause.
EN_WEAK: dict[str, list[str]] = {}
TR_WEAK: dict[str, list[str]] = {"flow": [r"\bnasil\b"]}
# (pattern, shadowable): a shadowable cue is also a common domain noun; it needs a
# second cue of the same intent when the word is part of this repository's vocabulary.
TR_CUES: dict[str, list[tuple[str, bool]]] = {
    "callers": [(r"\bkim(ler)? (cagir|kullan)\w*", False), (r"\bnere(ler)?den (cagri|cagril|kullanil)\w*", False),
                # passive: "kimler tarafından çağrılıyor", "hangi sınıflar tarafından kullanılıyor"
                # the verb only: not the participle "tarafindan kullanilan port" or the noun "cagri"
                (r"\btarafindan (cagril|kullanil)(?!an\b|dig|mis\b)\w*", False),
                (r"\bkim(ler)?\b[^.?!]{0,30}\b(cagril|kullanil)\w*", False),
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
    # "decide" is set below: DECIDE_STRONG_TR + DECIDE_TR, with vetoes
}
# -- decide: a choice the human makes (docs/DESIGN.md D33) --------------------------------------------
# Strong cues ask for a choice or a recommendation in so many words ("should we use", "would you
# recommend", "is X the right choice", "iyi bir fikir mi", "önerirsin"); they always read as a decision.
# The other cues ("should the X live in ...", "X or Y", "do we need a ...", "-meli miyiz", the optative)
# read as one only when no veto fires: a past tense ("why did we decide"), a question about what the
# code does ("how does X migrate", "what happens", "nasıl/nerede", a present-tense "-iyor"), or a usage
# verb ("hangi testi çalıştıralım"). A growth condition ("if traffic grows") is never a decision alone.
_TECH = (r"(?:sqlite\w*|postgres\w*|mysql|mariadb|mongo\w*|redis|memcached|duckdb|dynamodb|cassandra|kafka|"
         r"rabbitmq|celery|sqlalchemy|django|flask|fastapi|orm|fabric|neoforge|forge|quilt|spring|kotlin|java|"
         r"python|toml|yaml|json|xml|graphql|grpc|rest|nosql|sql)")
_CODE_NOUN_EN = (r"(?:functions?|methods?|class(?:es)?|files?|modules?|commands?|subcommands?|tests?|lines?|"
                 r"variables?|endpoints?|fields?|arguments?|args?|parameters?|values?|settings?|flags?|paths?|"
                 r"ports?|keys?|options?|scripts?|branch(?:es)?|names?|imports?|hooks?|tools?|env)")
# configuration the code states: "which Python version / database URL should I use" asks what the project
# uses ("which Django version should we move to" still asks for a choice)
_CONFIG_NOUN_EN = r"(?:versions?|urls?|uris?|dsns?|director(?:y|ies)|folders?|encodings?|interpreters?|locales?)"
_CHOICE_VERB_EN = (r"(?:use|switch|move|migrate|adopt|choose|pick|select|go with|replace|keep|stay|drop|introduce|"
                   r"add|split|merge|rewrite|port|upgrade|downgrade|prefer|standardi[sz]e|extract|separate|"
                   r"consolidate)")
DECIDE_STRONG_EN = [
    # "should we use ...", at the start of a clause or after "do you think" ("which value should I use" is not)
    r"(?:^|[,;:(]\s*|\b(?:or|and|so|then|now|also|think|believe|guess)\s+)should (?:we|i) "
    r"(?:(?:still|rather|instead|now|also|really|just) )?" + _CHOICE_VERB_EN + r"\b",
    r"\b(?:we|i) should (?:(?:still|rather|instead|now|also|really|just) )?" + _CHOICE_VERB_EN + r"\b",
    # a recommendation asked for
    r"\b(?:do|would|can|could|will) you (?:recommend|suggest|advise|propose)\b|\byour (?:recommendation|advice)\b",
    # a judgment of an option
    r"\bright (?:choice|call|tool|fit)\b|\bbetter (?:fit|choice|option|idea)\b|\boverkill\b|\ba good idea\b"
    r"|\bis it (?:wise|worth|time|better) to\b|\bis it worth\b|\bwould it (?:make sense|be better|be wise)\b"
    r"|\bdoes it make sense to\b|\bmakes? (?:more )?sense to\b|\bwhich (?:one )?is better\b",
    # "which X should we pick" (not "which function / value should I ...": the head noun is not code)
    r"\b(?:which|what) (?:[\w'-]+ ){0,2}(?!" + _CODE_NOUN_EN + r"\b)[\w'-]+ (?:should|shall) (?:we|i) "
    r"(?:choose|pick|select|adopt|go with|prefer|switch to|move to|migrate to|standardi[sz]e on)\b",
    r"\b(?:which|what) (?:[\w'-]+ ){0,2}(?!(?:" + _CODE_NOUN_EN + "|" + _CONFIG_NOUN_EN + r")\b)[\w'-]+ "
    r"(?:should|shall) (?:we|i) use\b",
    r"\b(?:should|must|shall|let's|lets|help (?:me|us)(?: to)?|(?:we|i) (?:need|have|want|ought) to|how (?:should|"
    r"shall) (?:we|i)) (?:choose|pick|select|decide) (?:between|among|whether|if|on|which)\b",
    r"\bwhat should (?:our|my|the) [\w' -]{1,40}? be\b",
    r"\bor do (?:we|i) need\b|\bor (?:should|shall) (?:we|i)\b",
    r"\bbest (?:way|approach|option|strategy) (?:to|for) (?:scale|scaling|store|storing|persist|deploy|host|structure|"
    r"organi[sz]e|split|migrate|cache|caching|queue)\b",
    r"\b(?:enough|sufficient) for (?:us|our|this|the (?:project|app|shop|team|load)|production|now)\b",
    # added after review round 3 (its 20 held-out choice questions are in-sample now)
    r"\bwould you (?:pick|choose|use|go with|prefer)\b|\bget away with\b|\bright (?:time|moment) (?:to|for)\b"
    r"|\bworth (?:the (?:effort|cost|trouble|risk|switch|move|migration)|it)\b|\bfits? (?:best|better)\b"
    r"|\bbest fit\b|\b(?:keep|stay with) (?:[\w'-]+ ){0,4}?or (?:replace|switch|move|migrate|drop|rewrite)\b",
]
DECIDE_EN = [
    # a clause that opens with "should": "should the key binding stay in ...", "should pricing be deployed ..."
    r"(?:^|[,;:(]\s*|\b(?:or|and|so|then|now|also)\s+)(?:should|shall) [\w.`'-]+\b"
    r"(?! (?:call|invoke|run|import|pass|look|see|read|check|expect)\b)",
    r"\bdo (?:we|i) (?:really |still )?need (?:a|an|another|more|to (?:add|introduce|switch|move|migrate|split|"
    r"replace))\b|\bor (?:just )?(?:keep|stay with|leave|switch to|move to|migrate to|go with)\b",
    r"\bhow (?:will|would|could|should) (?:[\w'-]+ ){0,8}?scale\b(?! with\b)|\bscalab\w*"
    r"|\bscale (?:up|out|horizontally|vertically|beyond)\b|\b(?:will|would|can|could) (?:it|this|that|the \w+) "
    r"scale\b(?! with\b)",
    r"\b" + _TECH + r"\b[^.?!]{0,40}?\b(?:or|vs\.?|versus|instead of|rather than|than)\b[^.?!]{0,40}?\b"
    + _TECH + r"\b",
    r"\b(?:pros and cons|trade-?offs?|advantages|disadvantages) (?:of|between|for)\b",
]
DECIDE_VETO_EN = [
    r"\b(?:did|was|were|had|has been|have been|recorded|listed|documented|wrote|written)\b",
    r"^\W*(?:how|where|when|what|which|why|who)\s+(?:does|do|is|are)\s+(?!(?:we|i|you)\b)",
    r"\bwhat happens\b|\bwhat (?:does|do) (?!(?:we|i|you)\b)",
    r"\b(?:does|do|is|are) (?:it|this|the (?:code|app|project|service|system))\b",
    r"\bhow (?:do|can|to) (?:i|we)\b",
]
_CODE_NOUN_TR = r"(?:fonksiyon|metot|metod|sinif|dosya|modul|komut|test|satir|degisken|parametre|alan|kod)"
_USAGE_VERB_TR = r"(?:calistir|cagir|oku|bak|incele|kontrol et|dene|test ed|derle|ac|kapat|sil)"
DECIDE_STRONG_TR = [
    r"\bsecmeli\w*|\bsecelim\b|\bsecmemiz (?:gerek|lazim)\w*",
    r"\bhangisini (?:kullan|sec|tercih)\w*|\bhangisi daha (?:iyi|uygun|dogru)\b",
    r"\biyi bir fikir m[iu]\b|\bdeger m[iu]\b|\bdaha (?:iyi|dogru|uygun|mantikli) olur\w*|\bmantikli\w*",
    r"\boner(?:ir|irsin|irsiniz|ebilir|ebilirsin|ebilirsiniz|iyor musun|iyor musunuz)\w*"
    r"|\btavsiye (?:eder|edersin|edersiniz|et)\w*",
    r"\byeterli m[iu]\b|\byeterli olur mu\b",
    r"\b(?:yuk|trafig|istek)\w* (?:\w+ )?(?:dayanir|kaldirir) m[iu]\b",
    r"\btercih et(?:meli|elim|memiz|mek)\w*",
    # growing the system, in the first person or with a modal: "nasıl büyütürüz", "ölçeklendirmeliyiz"
    r"\b(?:buyut|olcekle|olceklendir)\w*(?:uz|iz|elim|alim|meli\w*|mali\w*)\b",
    # added after review round 3: "... başlasak iyi olur mu", "taşınmanın tam zamanı mı"
    r"\biyi olur mu\b|\b(?:tam|dogru|uygun) zamani m[iu]\b",
]
DECIDE_TR = [
    r"\b\w{2,}m[ae]li\s+m[iu](?:y[iu]z|y[iu]m|s[iu]n|s[iu]n[iu]z)?\b|\b\w{2,}m[ae]l[iu]y[iu]z\b",
    # the optative in a question ("mı toplayalım yoksa ...", "hangi dilde yazalım?")
    r"\b(?!(?:bakalim|gorelim|gelelim|diyelim|anlayalim)\b)\w{2,}(?:y?alim|y?elim)\b(?=[^.!]*(?:\?|\bm[iu]\b|"
    r"\byoksa\b))|\b(?:hangi|m[iu])\b[^.!?]*\b(?!(?:bakalim|gorelim|gelelim|diyelim|anlayalim)\b)\w{2,}(?:y?alim|"
    r"y?elim)\b",
    # "geçsek mi", "kullansak mı"
    r"\b\w{2,}s[ae]k\s+m[iu]\b",
    r"\b" + _TECH + r"\w*(?:'\w+)?\s+m[iu]\b[^.?!]*\byoksa\b|\byoksa\b[^.?!]*\b" + _TECH,
    r"\bartilari\w*|\bavantajlari\w*|\bdezavantaj\w*",
]
DECIDE_VETO_TR = [
    r"\b(?:nasil|nerede|nereden|ne zaman|neden|niye|nicin|ne olur|ne oluyor)\b",
    r"\bhangi " + _CODE_NOUN_TR + r"\w*",
    r"\b" + _USAGE_VERB_TR + r"\w*(?:alim|elim|mali|meli|sak|sek)\w*",
    r"\b\w+(?:iyor|uyor)(?:lar|sa|mu|mi)?\b",
    r"\b\w+(?:mis|mus)(?:iz|uz|siniz|lar|tir|tur)?\b|\bkarar ver(?:dik|mistik)\b|\bsect(?:ik|iniz)\b",
]
EN_CUES["decide"] = DECIDE_STRONG_EN + DECIDE_EN
TR_CUES["decide"] = [(p, False) for p in DECIDE_STRONG_TR + DECIDE_TR]
_DECIDE_STRONG_EN_RX = [re.compile(p) for p in DECIDE_STRONG_EN]
_DECIDE_STRONG_TR_RX = [re.compile(p) for p in DECIDE_STRONG_TR]
_DECIDE_VETO_EN_RX = [re.compile(p) for p in DECIDE_VETO_EN]
_DECIDE_VETO_TR_RX = [re.compile(p) for p in DECIDE_VETO_TR]
# Primary-intent precedence when a clause carries several cues. A choice (decide) outranks everything:
# "should we move X from A to B" is a decision, not a flow question.
PRIORITY = ("decide", "callers", "performance", "why", "history", "impact", "tests", "compare_reference", "config",
            "usage", "flow", "behaviour", "locate", "dataflow", "define", "architecture")
_EN_RX = {k: [re.compile(p) for p in v] for k, v in EN_CUES.items()}
_EN_WEAK_RX = {k: [re.compile(p) for p in v] for k, v in EN_WEAK.items()}
_TR_WEAK_RX = {k: [re.compile(p) for p in v] for k, v in TR_WEAK.items()}
_TR_RX = {k: [(re.compile(p), s) for p, s in v] for k, v in TR_CUES.items()}

EN_INTERROGATIVES = frozenset("what where which how why who whom whose when does do is are can should could would "
                              "will did was were".split())
TR_INTERROGATIVES = frozenset("ne neyi neler nedir nerede nereye nereden neresi hangi hangisi hangileri nasil neden "
                              "nicin niye kim kimi kime kimin mi mu midir mudur miyim miyiz misin misiniz muyum "
                              "muyuz musun musunuz miydi muydu".split())
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


def asks_for_choice(text: str) -> bool:
    """Does ``text`` ask for a choice or a recommendation in so many words (a strong decide cue)?

    Such a question is the human's to decide whatever intent a plan gives it (docs/DESIGN.md D33)."""
    low = tn.nfc(text or "").lower()
    if any(rx.search(low) for rx in _DECIDE_STRONG_EN_RX):
        return True
    if not _uses_turkish_cues(text or ""):
        return False
    hits = [m for rx in _DECIDE_STRONG_TR_RX for m in [rx.search(tn.fold_tr(tn.nfc(text)))] if m]
    return bool(hits) and not (all(m.group(0).startswith("yeterli") for m in hits) and _enough_about_code(text))


_TECH_RX = re.compile(r"\b" + _TECH + r"\b")


def _enough_about_code(text: str) -> bool:
    """"<code> ... için yeterli mi" judges a name written as code ("validate_items boş siparişi reddetmek için
    yeterli mi?"), as English "is validate_items enough to ..." does: no choice between options, unless the
    clause names a technology or asks for us / under load ("bizim için", "yüke")."""
    folded = tn.fold_tr(tn.nfc(text or ""))
    if _TECH_RX.search(folded) or re.search(r"\bbizim icin\b|\b(?:yuk|trafik|buyume|olcek)\w*", folded):
        return False
    toks = [t.strip(".") for t in re.findall(r"`[^`]+`|[\w.]+(?:\(\))?", tn.nfc(text or ""))]
    return any(code_shape(t) for t in toks if re.search(r"[._(`]|[a-z][A-Z]", t))


def _enough_only(text: str) -> bool:
    """Is "yeterli mi" the only decision cue of the clause?"""
    if any(rx.search(tn.nfc(text).lower()) for rx in _EN_RX["decide"]):
        return False
    folded = tn.fold_tr(tn.nfc(text))
    hits = [m for rx, _shadow in _TR_RX["decide"] for m in [rx.search(folded)] if m]
    return bool(hits) and all(m.group(0).startswith("yeterli") for m in hits)


# words that may ask for a choice without a decide cue ("what would you pick", "fits best", "smarter"):
# only a note on the answer, never a verdict (tuned on in-sample questions: see docs/BENCHMARKS.md)
_MAYBE_CHOICE_EN = re.compile(r"\b(?:better|best|worth|wise|smart(?:er|est)?|recommend\w*|suggest\w*|advis\w*|"
                              r"prefer\w*|pick|choose|good idea|bad idea|overkill|enough|fits?|make sense|makes sense|"
                              r"instead of)\b|\bright (?:choice|call|way|approach|time)\b|\bor (?:keep|wait|stay)\b"
                              r"|\bvs\.?\s")
_MAYBE_CHOICE_TR = re.compile(r"\boner\w*|\btavsiye\w*|\btercih\w*|\bdaha iyi\b|\ben iyi\b|\bmantikli\w*|\bdogru "
                              r"(?:zaman|secim|karar)\w*|\bdeger mi\b|\byeterli\w*|\b\w{2,}s[ae]k\b[^.?!]*(?:\?|"
                              r"\bm[iu]\b)|\bm[iu]\b[^.?!]*\byoksa\b")


def may_ask_for_choice(text: str) -> str | None:
    """The word that may make ``text`` a request for a choice (no decide cue, no veto), else None."""
    if not text or _decide_vetoed(text):
        return None
    m = _MAYBE_CHOICE_EN.search(tn.nfc(text).lower())
    if m is None and _uses_turkish_cues(text):
        m = _MAYBE_CHOICE_TR.search(tn.fold_tr(tn.nfc(text)))
    return m.group(0).strip() if m else None


def _decide_vetoed(text: str) -> bool:
    low = tn.nfc(text).lower()
    if any(rx.search(low) for rx in _DECIDE_VETO_EN_RX):
        return True
    return _uses_turkish_cues(text) and any(rx.search(tn.fold_tr(tn.nfc(text))) for rx in _DECIDE_VETO_TR_RX)


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
    # a decide cue that is not a strong one does not survive a veto (past tense, a question about what the
    # code does, a usage verb): the clause keeps its other intents
    if "decide" in found and not asks_for_choice(text) and (_decide_vetoed(text) or _enough_only(text)):
        del found["decide"]
    # "who calls X" / "X kimler tarafından çağrılıyor": the call verb is the callers cue, not a flow one
    if "callers" in found and "flow" in found and \
            re.fullmatch(r"calls?|called|cagir\w*|cagri\w*", found["flow"]["cue"]):
        del found["flow"]
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
    re.compile(r",\s*(?:ayrica|bir de|peki|sonra|ardindan|daha sonra|ek olarak|bunun yaninda)\s+"),
    re.compile(r"\s+peki\s+|\s+bir de\s+|\s+hem de\s+"),
    re.compile(r",\s+(?=(?!yoksa\b)\w+(?:ysa|yse|sa|se)\b)"),  # "..., yoksa X mi?" is one choice
    re.compile(r",\s+(?=(?:ne|neyi|nerede|nereye|nereden|hangi|nasil|neden|kim)\b)"),
]
_CONDITIONAL = re.compile(r"^\s*\w+(?:ysa|yse|sa|se)\b|^\s*if (?:so|yes|it does|they do)\b")
_TR_VE = re.compile(r"\s+ve\s+")
_TR_COMMA = re.compile(r",\s+")
# the question particle with its person endings ("mi", "miyiz", "misiniz", "mudur"), folded
_TR_PARTICLE = re.compile(r"\bm[iu](?:y[iu]m|y[iu]z|s[iu]n|s[iu]n[iu]z|d[iu]r|yd[iu]|ym[iu]s)?\b")
# a clause that goes on after the comma (a condition, "-ken", "-ince") or offers an alternative ("yoksa")
_TR_COMMA_KEEP_LEFT = re.compile(r"(?:ysa|yse|sa|se|ken|ince|inca|unce|unca)\s*$")
_TR_COMMA_KEEP_RIGHT = re.compile(r"^\s*(?:yoksa|veya|ya da|ya)\b")


def _own_question(text: str, folded: str) -> set[str] | None:
    """The intents of a Turkish piece that asks its own question (a question word or particle, or a decision
    cue, and at least one intent cue), else None."""
    intents = {c["intent"] for c in clause_cues(text)}
    if intents and (_has_question_word(folded) or _TR_PARTICLE.search(folded) or "decide" in intents):
        return intents
    return None


def _carry_dropped_subjects(sqs: list[dict], mentions: list[dict]) -> None:
    """A later part that names no code asks about the code an earlier part named.

    Turkish drops the subject ("export.write nerede tanımlı, hem de hangi dosyaları okuyor?": which
    files does *it* read) and English can leave it out; a part with no code mention and at most one
    word of its own gets ``subject_from`` the nearest earlier part that names code. A part with its
    own words ("... and how are orders saved?") keeps its own subject."""
    ms = {m["id"]: m for m in mentions}
    names_code = [any(ms[x].get("kind") != "domain_concept" for x in sq["mentions"] if x in ms) for sq in sqs]
    for i in range(1, len(sqs)):
        sq = sqs[i]
        if sq.get("subject_from") or sq.get("depends_on") or names_code[i]:
            continue
        prev = next((j for j in range(i - 1, -1, -1) if names_code[j]), None)
        if prev is not None and len([x for x in sq["mentions"] if x in ms]) <= 1:
            sq["subject_from"] = sqs[prev]["id"]


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
    # A Turkish comma splits the same way when each side asks its own question ("SQLite yeterli mi,
    # place_order siparişi nasıl kaydediyor?", "PostgreSQL'e geçmeli miyiz, validate_items nereden
    # çağrılıyor?"), unless the left side is a condition, the right one an alternative ("..., yoksa") or both
    # sides are the options of one choice ("Fabric'e mi geçelim, NeoForge'da mı kalalım?").
    if _uses_turkish_cues(message):
        for m in _TR_COMMA.finditer(folded):
            if any(a == m.start() for a, _ in cuts):
                continue
            lo = max(b for b in bounds if b <= m.start())
            hi = min(b for b in bounds if b >= m.end())
            left, right = folded[lo:m.start()], folded[m.end():hi]
            if _TR_COMMA_KEEP_LEFT.search(left) or _TR_COMMA_KEEP_RIGHT.search(right):
                continue
            a_side, b_side = _own_question(message[lo:m.start()], left), _own_question(message[m.end():hi], right)
            if a_side and b_side and not ("decide" in a_side and "decide" in b_side):
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
        self.sites: dict[str, str | None] = {}   # name_site() results for this graph
        self.repo_files: list[str] | None = None
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
    from verinoda import lexicon as lexmod

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


# -- code-shaped names: an exact name, a verbatim occurrence, or not found --------------------------

def code_shape(text: str) -> str | None:
    """``"file"`` or ``"name"`` for a mention written as code (backticked, a path, snake_case, camelCase,
    dotted, ``name()``, UPPER_SNAKE), None for words. Such a mention names one thing exactly: it is
    never replaced by a merely similar name."""
    base = tn.split_apostrophe((text or "").strip())[0].strip()
    if not base or " " in base.strip("`").strip():
        return None
    kind = _code_kind(base)
    if kind is None or kind == "error_message" or base[:1] in "\"“":
        return None
    return "file" if kind == "file" else "name"


_IMPORT_LINE = re.compile(r"^\s*(?:from\s+[\w.]+\s+import\b|import\b)")
_TEXT_SUFFIXES_SKIP = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".jar", ".class", ".so", ".dll",
                       ".exe", ".pyc", ".woff", ".woff2", ".ttf", ".db", ".sqlite", ".bin", ".ogg", ".wav", ".mp3")
_SITE_MAX_BYTES = 1_000_000
_SITE_SECONDS = 2.0   # a scan that takes longer proves nothing: the name counts as spelled (UNCHECKED)
_PART_SECONDS = 0.25  # after a dotted name's last part is found, how long the whole name is still looked for
UNCHECKED = "(not checked: the repository is too large to scan)"


def _outside_imports(lines: list[str]) -> list[tuple[int, str]]:
    """``(line number, text)`` of the lines that are not import statements (``from x import (...)``,
    ``import {a, b} from 'y'`` blocks included): a name that only an import spells is defined nowhere here."""
    out = []
    close = None
    for i, ln in enumerate(lines, 1):
        if close is not None:
            if close in ln:
                close = None
            continue
        if _IMPORT_LINE.match(ln):
            for opener, closer in (("(", ")"), ("{", "}")):
                if opener in ln and closer not in ln.split(opener, 1)[1]:
                    close = closer
            continue
        out.append((i, ln))
    return out


def split_code_name(text: str) -> tuple[str, str]:
    """``(path, name)`` of a name written as code: ``orders/api.py::Cls.meth`` gives both, a path gives
    ``(path, "")``, ``Cls#meth`` gives ``("", "Cls.meth")``. Backticks, quotes, an apostrophe suffix, a
    trailing ``()`` and backslashes (Windows paths) are normalised away."""
    t = tn.split_apostrophe((text or "").strip())[0].strip().strip("`'\"").strip().replace("\\", "/")
    path, sep, name = t.rpartition("::")
    if not sep:
        path, name = "", t
    name = name.strip().replace("#", ".")
    name = name[:-2] if name.endswith("()") else name
    if not path and ("/" in name or PurePosixPath(name).suffix[1:].lower() in _CODE_EXTS):
        path, name = name, ""
    return path.strip().lstrip("./").strip("/"), name.strip().strip(".")


def _names_file(f: str, paths: set[str]) -> bool:
    """Is repository file ``f`` one of ``paths`` (a path suffix, with or without its extension)?"""
    stem = f.rpartition(".")[0] if "." in PurePosixPath(f).name else f
    return any(f == q or f.endswith("/" + q) or stem == q or stem.endswith("/" + q) for q in paths)


def _read_small(p: Path) -> str | None:
    try:
        if p.stat().st_size > _SITE_MAX_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _is_class(g, n: str) -> bool:
    return g.is_symbol(n) and not g.label(n).rstrip().endswith(")")


def _lines_of(g, f: str) -> list[str]:
    data = _read_small(Path(g.root) / f)
    return data.splitlines() if data is not None else []


def _class_header(lines: list[str], a: int, b: int) -> str:
    """A class's header: the decorator/annotation lines right above line ``a`` and its lines up to the
    one that opens the body (``{``, or a line ending with ``:``)."""
    deco = []
    for ln in reversed(lines[max(0, a - 4):a - 1]):
        if not ln.strip().startswith("@"):
            break
        deco.append(ln)
    head = []
    for ln in lines[a - 1:min(b, a + 4)]:
        head.append(ln)
        if "{" in ln or ln.rstrip().endswith(":"):
            break
    return " ".join([*reversed(deco), *head])


def _has_bases(f: str, header: str) -> bool:
    """Does a class header (its first lines, with the lines before it) name a base class, or a decorator
    or annotation (``@dataclass``, Lombok's ``@Data``), ``data``/``case``/``partial`` class, so members
    may be inherited or generated?"""
    if re.search(r"(?:^|\s)@\w|\b(?:data|case|partial)\s+class\b", header):
        return True
    if f.endswith((".py", ".pyi")):
        m = re.search(r"\bclass\s+\w+\s*(?:\[[^\]]*\]\s*)?\(([^)]*)", header)
        return bool(m) and m.group(1).strip() not in ("", "object")
    head = header.split("{")[0]
    return bool(re.search(r"\b(?:extends|implements|with)\b|\)\s*:\s*[A-Za-z]|\bclass\s+\w+(?:<[^>]*>)?\s*:\s*[A-Za-z]",
                          head))


_PY_EXTS = (".py", ".pyi")
# A Python class body that writes attributes by name, or reads unknown ones: its members are not all
# spelled in it (`self.__dict__.update(kw)`, `setattr(self, k, v)`, `__getattr__`).
_DYNAMIC_MEMBERS = re.compile(r"__getattr|__setattr__|\bsetattr\s*\(|__dict__|\bvars\s*\(")
# A module whose names may come from elsewhere or be made at runtime.
_DYNAMIC_MODULE = re.compile(r"\bimport\s+\*|\bexport\s+\*|^def\s+__getattr__|\bglobals\s*\(\s*\)|\bsys\.modules\b|"
                             r"\bsetattr\s*\(", re.M)
# What every Java object has from java.lang.Object (Python's are the dunders): never absent from a class.
_JAVA_OBJECT_MEMBERS = frozenset("toString equals hashCode getClass notify notifyAll wait clone finalize".split())
# Data and configuration files: a key there may be what a dotted name means (`pricing.discount_rate`).
_DATA_SUFFIXES = (".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".conf", ".properties", ".env", ".xml")


class _OutOfTime(Exception):
    """A scan went past its deadline: nothing is claimed about the name (UNCHECKED)."""


def _reader(g, deadline: float | None):
    """A file reader for one lookup: each file read once; past ``deadline`` it raises :class:`_OutOfTime`."""
    cache: dict[str, list[str]] = {}

    def read(f: str) -> list[str]:
        if f not in cache:
            if deadline is not None and time.perf_counter() > deadline:
                raise _OutOfTime
            cache[f] = _lines_of(g, f)
        return cache[f]

    return read


def _owner_scopes(g, ix: _Index, owner: str, read=None) -> tuple[list[tuple[str, int, int, str]], bool]:
    """Where the owner of a dotted name (``Owner.member``) is defined in the graph: ``(scopes, open)``.

    ``scopes`` are ``(file, start, end, kind)``: the classes named ``owner`` (``"class"``, their
    lines; another letter case only when no class has this one: ``cart`` for ``Cart``) and the code
    modules it names (``"module"``, the whole file, imports included - they re-export: ``service``,
    ``orders.config``; a package: every module in it; a directory of that name, as a Go or Java
    package: every code file in it). ``open`` when a member may be defined elsewhere, so a member
    missing from the scopes is not absent: an owner matched only in another letter case (``cart`` is
    a variable or a section, not the class), a class in a language other than Python and Java, or a
    Java interface, enum or record (extension functions, methods in other files, generated members),
    a class with a base class, a decorator or annotation, a Python class with dynamic attributes
    (``__getattr__``, ``setattr``, ``__dict__``), a module with a star import or runtime names, a
    non-Python module, a directory package, a file that cannot be read."""
    read = read or _reader(g, None)
    last = owner.rpartition(".")[2]
    classes = [n for n in ix.by_bare.get(last, ()) if _is_class(g, n)]
    is_open = False
    if not classes and _compact(last):
        classes = [n for n in ix.by_compact.get(_compact(last), ()) if _is_class(g, n)]
        is_open = bool(classes)
    scopes: list[tuple[str, int, int, str]] = []
    for n in classes:
        f, sp = g.file(n), g.span(n)
        lines = read(f) if f else []
        if not (f and sp and lines):
            is_open = True
            continue
        a, b = sp
        header = _class_header(lines, a, b)
        if f.endswith(_PY_EXTS):
            closed = not _DYNAMIC_MEMBERS.search("\n".join(lines[a - 1:b]))
        else:  # a Java class (not an interface, enum or record) declares its members in its body
            closed = f.endswith(".java") and bool(re.search(rf"\bclass\s+{re.escape(g.label(n))}\b", header))
        if not closed or _has_bases(f, header):
            is_open = True
        scopes.append((f, a, b, "class"))
    path = owner.replace(".", "/")
    # a package holds what its modules define (`orders.place_order` for orders/service.py)
    pkgs = tuple({f.rpartition("/")[0] + "/" for f in ix.files if _names_file(f, {f"{path}/__init__"})})
    for f, n in ix.files.items():
        if g.G.nodes[n].get("file_type") != "code":
            continue
        parent = f.rpartition("/")[0]
        named = _names_file(f, {path}) or (bool(pkgs) and f.startswith(pkgs))
        if not (named or parent == path or parent.endswith("/" + path)) or any(s[0] == f for s in scopes):
            continue  # (a file named like a class it holds, `Wisp.java`: the class is the owner)
        lines = read(f)
        if not lines:
            is_open = True
            continue
        if not (named and f.endswith(_PY_EXTS)) or _DYNAMIC_MODULE.search("\n".join(lines)):
            is_open = True
        scopes.append((f, 1, len(lines), "module"))
    return scopes, is_open


def _py_class_members(text: str, a: int, b: int) -> dict[str, int] | None:
    """The names a Python class defines, with the line of the first binding: its defs and nested
    classes, class-level assignments (``MAX_RETRIES = 5``, ``x: int``, ``__slots__`` entries, imports)
    and the attributes its methods assign on their first parameter (``self.conn = ...``). A call on
    another object (``self.conn.execute(...)``) is not a member. None when the file does not parse or
    no class starts within lines a-b."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    found = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and a <= n.lineno <= b]
    if not found:
        return None
    out: dict[str, int] = {}

    def target(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            out.setdefault(t.id, t.lineno)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                target(e)
        elif isinstance(t, ast.Starred):
            target(t.value)

    def method(fn: ast.AST) -> None:
        params = [*fn.args.posonlyargs, *fn.args.args]
        if not params:
            return
        me = params[0].arg
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)) and \
                    isinstance(n.value, ast.Name) and n.value.id == me:
                out.setdefault(n.attr, n.lineno)

    def body(stmts: list) -> None:
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.setdefault(st.name, st.lineno)
                if not isinstance(st, ast.ClassDef):
                    method(st)
            elif isinstance(st, ast.Assign):
                for t in st.targets:
                    target(t)
                    if isinstance(t, ast.Name) and t.id == "__slots__":
                        for e in getattr(st.value, "elts", [st.value]):
                            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                                out.setdefault(e.value, st.lineno)
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                target(st.target)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for al in st.names:
                    out.setdefault((al.asname or al.name).split(".")[0], st.lineno)
            elif isinstance(st, (ast.For, ast.AsyncFor)):
                target(st.target)
                body(st.body)
                body(st.orelse)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                for it in st.items:
                    if it.optional_vars is not None:
                        target(it.optional_vars)
                body(st.body)
            elif isinstance(st, (ast.If, ast.While)):
                body(st.body)
                body(st.orelse)
            elif hasattr(st, "handlers") and hasattr(st, "finalbody"):  # try (and try/except* on 3.11+)
                body(st.body)
                for h in st.handlers:
                    body(h.body)
                body(st.orelse)
                body(st.finalbody)
            elif isinstance(st, ast.Match):
                for case in st.cases:
                    body(case.body)

    body(min(found, key=lambda n: n.lineno).body)
    return out


def _member_site(g, ix: _Index, name: str, *, deadline: float | None = None) -> tuple[str | None, bool]:
    """``(site, decided)`` for a dotted name whose owner the graph defines: the member inside the owner
    (``file:line``; a Python class's member is one it defines, see :func:`_py_class_members`; other
    owners: the word anywhere in their lines), or None when it is not there - ``decided`` is False
    when the owner is not a class or module of the graph, a member may come from elsewhere (see
    :func:`_owner_scopes`), or the member is a dunder every object has (``__repr__``). Past
    ``deadline``: ``(UNCHECKED, True)``."""
    owner, _, member = name.rpartition(".")
    if not owner or not member:
        return None, False
    read = _reader(g, deadline)
    try:
        scopes, is_open = _owner_scopes(g, ix, owner, read)
        if not scopes:
            return None, False
        rx = re.compile(rf"(?<![\w]){re.escape(member)}(?![\w])", re.I)
        for f, a, b, kind in scopes:
            if deadline is not None and time.perf_counter() > deadline:
                raise _OutOfTime
            lines = read(f)
            if kind == "class" and f.endswith(_PY_EXTS):
                members = _py_class_members("\n".join(lines), a, b)
                if members is not None:
                    hit = members.get(member) or next((ln for k, ln in members.items()
                                                       if k.lower() == member.lower()), None)
                    if hit is not None:
                        return f"{f}:{hit}", True
                    continue
                is_open = True  # its members cannot be read: the word counts
            chunk = "\n".join(lines[a - 1:b])
            m = rx.search(chunk)  # one search per scope; the line only when it is there
            if m is not None:
                return f"{f}:{a + chunk.count(chr(10), 0, m.start())}", True
    except _OutOfTime:
        return UNCHECKED, True
    inherited = (member.startswith("__") and member.endswith("__")) or member in _JAVA_OBJECT_MEMBERS
    return None, not is_open and not inherited


def _member_elsewhere(g, ix: _Index, name: str, deadline: float) -> str | None:
    """Where the repository gives a closed owner's missing member anyway (``file:line``): the whole
    dotted name, an attribute assignment ``x.member = ...``, ``setattr(..., "member", ...)``, or a key
    ``member`` in a data or configuration file (``discount_rate: 0.1`` under ``pricing:``). None when
    none of them occurs; UNCHECKED past ``deadline``."""
    member = name.rpartition(".")[2]
    word = re.compile(rf"(?<![\w]){re.escape(member)}(?![\w])", re.I)
    whole = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])", re.I)
    code = re.compile(rf"\.\s*{re.escape(member)}\s*(?::[^=\n]*)?=(?!=)|\bsetattr\s*\([^)\n]*['\"]{re.escape(member)}"
                      r"['\"]")
    key = re.compile(rf"(?:^|[\s{{,\[])[\"']?{re.escape(member)}[\"']?\s*[:=]", re.I)
    for f in _files_to_scan(g, ix, [member]):
        if f.lower().endswith(_TEXT_SUFFIXES_SKIP):
            continue
        if time.perf_counter() > deadline:
            return UNCHECKED
        data = _read_small(Path(g.root) / f)
        if data is None or not word.search(data):
            continue
        is_data = f.lower().endswith(_DATA_SUFFIXES)
        for i, ln in _outside_imports(data.splitlines()):
            if whole.search(ln) or (key if is_data else code).search(ln):
                return f"{f}:{i}"
    return None


def owner_known(g, text: str) -> bool:
    """Is the owner of a dotted code name (``Owner.member``) a class, module, package or directory of
    the graph? (Past the scan's time limit: assumed so, which claims nothing.)"""
    owner = split_code_name(text)[1].rpartition(".")[0]
    if not owner:
        return False
    try:
        return bool(_owner_scopes(g, _index(g), owner, _reader(g, time.perf_counter() + _SITE_SECONDS))[0])
    except _OutOfTime:
        return True


def _member_near(g, ix: _Index, text: str) -> list[tuple[str, float]]:
    """Near misses of a dotted name ``Owner.member``: the owner's members with a similar name
    (``Cart._check`` for ``Cart.check``), then symbols named ``member`` elsewhere (``place_order`` for
    ``OrderRepository.place_order``)."""
    import difflib

    owner, _, member = split_code_name(text)[1].rpartition(".")
    if not owner or not member:
        return []
    out: list[tuple[str, float]] = []
    last = owner.rpartition(".")[2]
    for c in [n for n in ix.by_bare.get(last, ()) if _is_class(g, n)]:
        for v, _ in g.out_edges(c, {"method"}):
            r = difflib.SequenceMatcher(None, member.lower(), _bare(g.label(v)).lower()).ratio()
            if r >= 0.75:
                out.append((v, round(100 * r, 1)))
    out.sort(key=lambda x: -x[1])
    out += [(n, 100.0) for n in ix.by_bare.get(member, ()) if g.is_symbol(n) and n not in {m for m, _ in out}]
    return out[:4]


def spells_whole(g, site: str | None, text: str) -> bool:
    """Does the line at ``site`` (``file:line``) spell the whole code name ``text`` (any letter case)?"""
    if not site or ":" not in site:
        return bool(site)
    f, _, ln = site.rpartition(":")
    name = split_code_name(text)[1]
    lines = _lines_of(g, f) if ln.isdigit() else []
    if not name or not 0 < int(ln or 0) <= len(lines):
        return False
    return bool(re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", lines[int(ln) - 1], re.I))


def name_site(g, text: str, *, strict: bool = False) -> str | None:
    """Where the repository spells a code-shaped name outside import statements (``file:line``), or a
    file with that path; None when it spells it nowhere (then the name does not exist here).

    Lenient on purpose: a dotted name counts when its last part occurs (unless ``strict``: then the
    whole name must occur), any letter case counts. ``path::name`` looks for the name in that file
    only. A dotted name whose owner is a class or module of the graph that cannot get members from
    elsewhere (``OrderRepository.place_order``) is looked for inside that owner (:func:`_member_site`)
    and then as a whole, an attribute assignment, ``setattr`` or a data key elsewhere
    (:func:`_member_elsewhere`), not by its last part alone. Only a name found nowhere is reported as
    not found; a lookup that runs past ``_SITE_SECONDS`` is UNCHECKED.
    """
    ix = _index(g)
    path, name = split_code_name(text)
    if not (path or name):
        return None
    key = f"{path}::{name}::{int(strict)}"
    if key in ix.sites:
        return ix.sites[key]
    if ix.repo_files is None:
        from verinoda.snapshot import list_files

        try:
            ix.repo_files = list_files(Path(g.root))
        except (OSError, ValueError):
            ix.repo_files = sorted(ix.files)
    site: str | None = None
    if path:  # a file, or a name in that file
        f = next((f for f in ix.repo_files if _names_file(f, {path})), None)
        if f is not None and not name:
            site = f
        elif f is not None:
            rx = re.compile(rf"(?<![\w]){re.escape(name.rpartition('.')[2])}(?![\w])")
            data = _read_small(Path(g.root) / f) or ""
            hit = next((i for i, ln in _outside_imports(data.splitlines()) if rx.search(ln)), None)
            site = f"{f}:{hit}" if hit is not None else None
        ix.sites[key] = site
        return site
    as_paths = {name} | ({name.replace(".", "/")} if "." in name else set())
    site = next((f for f in ix.repo_files if _names_file(f, as_paths)), None)
    spent = 0.0  # the owner's lookup and the scan share one limit (_SITE_SECONDS)
    if site is None and "." in name and not strict:
        t0 = time.perf_counter()
        member, decided = _member_site(g, ix, name, deadline=t0 + _SITE_SECONDS)
        if member is not None or decided:
            # the owner is a class or module here: its member, or the member given to it elsewhere
            site = member if member is not None else _member_elsewhere(g, ix, name, t0 + _SITE_SECONDS)
            ix.sites[key] = site
            return site
        spent = time.perf_counter() - t0
    if site is None:
        last = name.rpartition(".")[2] if "." in name and not strict else None
        whole = [re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])"),
                 re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])", re.I)]
        part = re.compile(rf"(?<![\w]){re.escape(last)}(?![\w])") if last and len(last) >= 3 else None
        pats = whole + ([part] if part else [])
        files = _files_to_scan(g, ix, [name] if strict or not last else [name, last])
        deadline = time.perf_counter() + _SITE_SECONDS - spent
        # the whole name is preferred to its last part (`repo.save` over `def save`), for a short while
        part_hit, part_deadline = None, None
        for f in files:
            if f.lower().endswith(_TEXT_SUFFIXES_SKIP):
                continue
            now = time.perf_counter()
            if now > deadline or (part_deadline is not None and now > part_deadline):
                site = part_hit or UNCHECKED
                break
            data = _read_small(Path(g.root) / f)
            if data is None or not any(rx.search(data) for rx in pats):
                continue
            for i, ln in _outside_imports(data.splitlines()):
                if any(rx.search(ln) for rx in whole):
                    site = f"{f}:{i}"
                    break
                if part_hit is None and part is not None and part.search(ln):
                    part_hit, part_deadline = f"{f}:{i}", now + _PART_SECONDS
            if site is not None:
                break
        site = site or part_hit
    ix.sites[key] = site
    return site


def _files_to_scan(g, ix: _Index, names: list[str]) -> list[str]:
    """The repository files that may spell one of ``names``: every file, unless the search index
    (``search.db`` of this graph) shows that no indexed file has every word of any of the names - then
    only the files it does not index and those changed since it indexed them. (Import lines are not
    indexed; name_site ignores them anyway.)"""
    files = ix.repo_files or []
    try:
        from verinoda import search_index as si
        from verinoda.index import same_graph

        db = si.db_path_for(g)
        if not db.exists():
            return files
        conn = sqlite3.connect(str(db), timeout=5)
        try:
            m = si._meta(conn)
            if not (m.get("schema_version") == si.SCHEMA_VERSION and m.get("tokenizer_version") == si.TOKENIZER_VERSION
                    and same_graph(g.path, m.get("graph"))):
                return files
            def indexed_word(w: str) -> bool:  # a file word the name_site patterns match has one of these tokens
                toks = si.word_tokens(w)
                forms = {toks[0], tn.fold_tr(w.strip("_"))} if toks else set()
                return not forms or any(conn.execute("SELECT 1 FROM df WHERE term = ?", (t,)).fetchone()
                                        for t in forms)

            for name in names:
                if all(indexed_word(w) for w in re.findall(r"\w+", name)):
                    return files  # an indexed file may spell it: scan them all
            indexed = {r[0]: (r[1], r[2]) for r in
                       conn.execute("SELECT file, size, mtime_ns FROM files WHERE skipped IS NULL")}
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ImportError, ValueError):
        return files
    out = []
    for f in files:
        if f in indexed:
            try:
                st = (Path(g.root) / f).stat()
            except OSError:
                continue
            if (st.st_size, st.st_mtime_ns) == indexed[f]:
                continue  # indexed as it is now: its words are in the index
        out.append(f)
    return out


def _exact_name(scored: dict[str, dict]) -> bool:
    """Did the mention's own text match some node at a name tier? (A host candidate that differs from
    what the user wrote is the host's reading, not the user's name: it does not count.)"""
    return any(m["type"] in NAME_TIERS and m["via"] == "text" for info in scored.values() for m in info["matches"])


def not_found_line(g, text: str, near: list[dict]) -> str:
    """"no symbol named `x` in this repository; nearest: y (file:line)"."""
    shape = code_shape(text) or "name"
    name = tn.split_apostrophe((text or "").strip())[0].strip().strip("`")
    line = f"no {'file' if shape == 'file' else 'symbol'} named `{name}` in this repository"
    path, sym = split_code_name(text)
    if path and sym:  # `path::name`: the file, or the name in it
        known = [f for f in _index(g).files if _names_file(f, {path})]
        line = f"no symbol named `{sym}` in {known[0]}" if known else f"no file named `{path}` in this repository"
    if near:
        line += "; nearest: " + ", ".join(f"{_bare(n['label'])} ({n['site']})" for n in near[:2])
    return line


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
    from verinoda import evidence as evmod
    from verinoda.index import _file_lines

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
    phrase_hits = getattr(lexicon, "phrase_hits", None)
    for hit in phrase_hits(folded_words) if callable(phrase_hits) else []:
        for t in hit["targets"]:  # a Turkish label from the locale files names its key's identifier
            out.append((t, f"lexicon:label '{hit['key']}'", TIER_SCORES["fuzzy"]))
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
    shape = code_shape(mention.get("text") or "")
    if shape and not _exact_name(scored):
        # A name written as code is never replaced by a similar one: without an exact name it is
        # either spelled somewhere in the repository (a key, a string, an external name: the link
        # below stays, at most weak) or it does not exist here.
        site = name_site(g, mention.get("text") or "")
        if site is None:
            near = _member_near(g, ix, mention.get("text") or "")
            near += [x for x in _near_misses(ix, mention.get("text") or "") if x[0] not in {n for n, _ in near}]
            link["status"] = "not_found"
            link["near_misses"] = [{"node": n, "label": g.label(n), "at": _at(g, n), "site": _site(g, n),
                                    "similarity": round(s, 1)} for n, s in near]
            link["did_you_mean"] = [f"{_bare(nm['label'])} ({nm['site']})" for nm in link["near_misses"]]
            link["not_found"] = not_found_line(g, mention.get("text") or "", link["near_misses"])
            return link
        if site == UNCHECKED:  # existence not checked: never linked by a merely similar name
            link["existence"] = "unchecked"
        else:
            link["occurs_at"] = site
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
    if best["score"] < THRESHOLDS["link"] or link.get("occurs_at") or link.get("existence"):
        link["status"] = "weak"
        link["uncertainty"] = f"'{mention.get('text')}' linked only by {link['tier']} (score {best['score']:.2f})"
        written = (mention.get("text") or "").strip().strip("`")
        if link.get("occurs_at"):
            # a dotted name found by its last part: say which part occurs, not "the name"
            where = (f"the name occurs at {link['occurs_at']}" if spells_whole(g, link["occurs_at"], written)
                     else f"`{split_code_name(written)[1].rpartition('.')[2]}` occurs at {link['occurs_at']}, not the "
                          "whole name")
            link["uncertainty"] = (f"no symbol in the index is named `{written}` ({where}); linked by "
                                   f"{link['tier']} to {g.label(best['best'])}, not by its name")
        elif link.get("existence"):
            link["uncertainty"] = (f"no symbol in the index is named `{written}`, and whether the repository spells "
                                   "it was not checked (the repository is too large to scan); linked by "
                                   f"{link['tier']} to {g.label(best['best'])}, not by its name")
        link["nodes"] = [f["best"] for f in fams if best["score"] - f["score"] < THRESHOLDS["margin"]][:5]
    elif len(close) == 1:
        link["status"] = "linked"
        link["nodes"] = [best["best"]]
        written, label = split_code_name(mention.get("text") or "")[1], _bare(g.label(best["best"]))
        if shape and link["tier"] == "label_folded" and tn.fold_tr(written) != tn.fold_tr(label):
            # `placeOrder` for place_order: the same letters in another spelling - said, never silent
            link["status"] = "weak"
            link["uncertainty"] = (f"`{written}` is spelled `{label}` here: linked by its letters (label_folded), "
                                   "not by its name")
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
    from verinoda import architecture_map as am

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
    from verinoda.snapshot import git

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
                rc["next_step"] = f"`verinoda resolve \"{r.get('text')}\"` (no local tag named {spec})"
        elif r.get("kind") in ("file", "local_path", "symbol") and not spec and ix is not None:
            loc = r.get("locator") or r.get("text") or ""
            hit = _match_string(graph, ix, loc) if loc else []
            rc["status"] = "resolved" if hit else "unresolved"
            rc["version_used"] = "working tree"
            rc["evidence" if hit else "next_step"] = (f"found locally: {_at(graph, hit[0][0])}" if hit else
                                                      "give the path as it appears in the repository")
        else:
            rc["status"] = "pending_research"
            rc["next_step"] = (f"`verinoda resolve \"{r.get('text')}\"`" + ("" if spec else
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
        elif lk["status"] in ("unlinked", "not_found") and m.get("required", True) and not _is_concept(m) \
                and lk.get("near_misses"):
            opts = [{"value": nm["node"], "label": f"{nm['label']} ({nm['at']})",
                     "evidence": f"similar name ({nm['similarity']})"} for nm in lk["near_misses"][:4]]
            out.append(_clarification(plan, f"c-{lk['mention']}", lk["mention"], "did_you_mean", lk["text"], opts,
                                      lk.get("not_found") or "no exact match; similar names exist",
                                      _blocks(plan, lk["mention"])))
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
        result["errors"] = [_problem("/", "schema", "a plan is a JSON object", "see `verinoda plan schema`")]
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
        for lk in links:  # a name that does not exist comes first: nothing about it can be answered
            if lk["status"] == "not_found" and mentions[lk["mention"]].get("required", True):
                result["unknowns"].append({
                    "about": lk["mention"], "question": f"what is `{lk['text']}` in this repository?",
                    "why": lk["not_found"],
                    "next_step": "use a name as written in the code" + (f" (did you mean {lk['did_you_mean'][0]}?)"
                                                                         if lk.get("did_you_mean") else "")
                                 + ", or check whether it comes from a library (`verinoda research`)"})
        for lk in links:
            m = mentions[lk["mention"]]
            if lk["status"] == "unlinked" and m.get("required", True) and not lk.get("near_misses"):
                result["unknowns"].append({
                    "about": lk["mention"], "question": f"what is '{lk['text']}' in this repository?",
                    "why": f"no graph node, file or lexicon entry matches '{lk['text']}'",
                    "next_step": "give the identifier or path as written in the code, or browse with "
                                 "`verinoda map` / `verinoda query`"})
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
        if best and lk["status"] not in ("unlinked", "not_found"):
            item.update({"at": best.get("at"), "label": best.get("label"), "score": best.get("score"),
                         "tier": lk.get("tier")})
        if lk["status"] == "not_found":
            item.update({"not_found": lk.get("not_found"), "did_you_mean": lk.get("did_you_mean") or []})
        elif lk.get("occurs_at"):
            item["occurs_at"] = lk["occurs_at"]
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
    from verinoda.store import new_id, now

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

    # multi-word seed phrases ("ev dizininde" -> home) are read before the short-word
    # filter below would drop a part of them ("ev")
    phrase_at: dict[int, tuple[int, str]] = {}
    if lex is not None and lang in ("tr", "mixed"):
        folded_toks = [tn.fold_tr(tn.split_apostrophe(t["text"])[0]).lower() for t in toks]
        for hit in lex.seed(folded_toks):
            if hit["n"] >= 2 and hit["targets"]:
                phrase_at[hit["start"]] = (hit["n"], hit["targets"][0])
    pending: list[dict] = []
    skip_to = 0
    for ti, tok in enumerate(toks):
        if ti < skip_to or any(a <= tok["start"] < b for a, b in ref_spans):
            continue
        if ti in phrase_at:
            n, g_en = phrase_at[ti]
            last = toks[ti + n - 1]
            if clause_of(last["start"]) == clause_of(tok["start"]) \
                    and not any(a < last["end"] and tok["start"] < b for a, b in ref_spans):
                ptok = {"text": question[tok["start"]:last["end"]], "start": tok["start"], "end": last["end"]}
                pending.append({"tok": ptok, "kind": "domain_concept", "required": False,
                                "clause": clause_of(tok["start"]), "gloss": g_en, "role_tok": last})
                skip_to = ti + n
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
        role = role_at(p.get("role_tok") or tok)
        if role:
            m["role"] = role
        if lex is not None and lang in ("tr", "mixed"):
            g_en = p.get("gloss") or _gloss(lex, tn.split_apostrophe(tok["text"])[0])
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
    _carry_dropped_subjects(sqs, mentions)
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
    words, seq = [], []  # content words; every word in order (multi-word seed keys need adjacency)
    for t in tn.raw_tokens(text + " " + (user_text if user_text != text else "")):
        for w in re.findall(r"[a-z0-9_]+", tn.fold_tr(t).lower()):
            seq.append(w)
            if len(w) >= 3 and w not in tn.TR_STOPWORDS and w not in tn.EN_STOPWORDS and w not in words:
                words.append(w)
    if lexicon is not None:
        for hit in lexicon.seed(seq):
            src = " ".join(seq[hit["start"]:hit["start"] + hit["n"]])
            if hit["n"] == 1 and src not in words:
                continue  # a single short or stop word keeps being ignored
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
