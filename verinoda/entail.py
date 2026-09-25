"""Mechanical entailment grades: does this evidence support this claim? (docs/DESIGN.md D26)

``grade(kind, repo, spec, evidence_row) -> "full" | "partial" | "none"`` is the
AIS question "according to this evidence, is the claim true?" answered from
source, AST and run metadata, with Self-RAG's three support levels:

* ``full``    the evidence mechanically states the claim;
* ``partial`` the evidence is about the claim's subject but does not state it;
* ``none``    the evidence is not relevant to the claim.

:func:`verinoda.claims.check_status` requires a fully graded evidence group
for every ``*_verified`` status and at least one relevant (``partial``)
evidence for ``strong_inference``, so irrelevant evidence can never verify a
claim, whatever path attached it.

Per claim kind:

* ``relation`` (``spec.at``, ``spec.target_label``): full when the AST has a
  call at the cited line whose callee is the target's name or an import alias
  of it (bound to the target's module when the import says which), inside the
  claimed caller and not rebound locally; ``inherits``/``imports`` edges need
  the class base or import statement. A method call on an object whose type is
  not resolved statically (``repo.save()``) is ``partial``; ``self.m()`` inside
  the target's class is ``full``. A graph edge with the claim's endpoints is
  ``partial``; a static resolver's definitive answer naming the target, or an
  observed call (runtime trace) at the site, is ``full``.
* ``location``: full when a definition (or Markdown section) with the claimed
  name starts at the cited line and ends at the cited end line.
* ``config`` (``spec.env``): full for an environment read of that literal at
  the line (``os.environ.get``/``getenv``/``os.environ[...]``, or the
  architecture map's patterns for other languages).
* ``flow``: each hop line graded like a relation; sink lines by the sink
  pattern the claim names.
* ``exclusive`` (``spec.pattern``): full when the cited line matches it.
* ``behaviour``: an order claim (``spec.proposition`` "A before B in F" with
  ``spec.holds``) is full when, in the whole body of the Python definition F,
  the first call of the call stated first comes before the first call of the
  other one (:func:`call_order`); a pattern claim (``spec.pattern``) like
  ``exclusive``.
* ``test_run``: full only when the claim spec names the run (``spec.experiment``
  or all ``spec.command`` ids in its command line) and every claim subject is
  exercised by it (test files in the command; code reached through the test
  files' in-repo imports).
* ``tests``, ``impact``: ``partial`` at most (static reachability is inference).
* ``decision`` / ``history``: ``partial`` at most - a document's rationale
  cannot be entailed mechanically. ``partial`` needs *attribution*: the record
  must state every content term the claim attributes to it (for history, the
  claim must name the commit); anything less is ``none``.
* anything else (``general``): term coverage, which is ``partial`` at most:
  every key term (identifiers, numbers, product names such as ``PostgreSQL``
  or ``AES-256``) and every content word of the claim appearing in the
  evidence (cited lines, enclosing symbol, file name; for a run: its command
  and summary) makes the evidence relevant, never a verification, because
  order, direction, conditions and roles are invisible to word overlap
  ("apply_discount returns the subtotal above the threshold" has every word
  of the lines that return ``subtotal * 0.9`` there). Only a verbatim quote
  (``contains: <text>``) or a kind's typed check is ``full``. This grade is a
  labelled heuristic (``Grade.reason`` says so, ``Grade.code`` is
  ``coverage``).

Text written by a user or an agent (``spec.free_text``, ``claim add``) must
also bind every role it states (:func:`assess`): it names the kind's symbol;
a relation's text states the caller and the callee in that direction
(:func:`relation_roles`); a config claim's subject is the name the
environment read is bound to (:func:`env_bindings`).
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from verinoda import anchors
from verinoda import evidence as evmod
from verinoda.textnorm import EN_STOPWORDS, SHORT_TECH, TR_STOPWORDS, fold_tr, split_identifier

GRADES = ("none", "partial", "full")
KIND_MAX = {"decision": "partial", "history": "partial", "tests": "partial", "impact": "partial"}
# Documentary kinds: their best mechanical grade is attribution ("the record states it").
DOCUMENTARY = ("decision", "history")

# Words that describe the claim's shape, not its content.
LIGHT = frozenset("""
call calls called calling invoke invokes invoked uses use used using via through defined defines define
definition contains contain containing located lives live found file files line lines function functions
method methods class classes module modules code statement statements variable value exists exist also
decision record records explains explain status changed change commit commits here there then just
read reads reading
""".split())
NEGATIONS = frozenset("""not never no none nothing without cannot cant can't doesnt doesn't dont don't isnt isn't
arent aren't wont won't neither nor degil yok hic asla olmadan""".split())
QUANTIFIERS = frozenset("only all every always each solely exclusively sadece yalniz yalnizca tum hep her".split())

_LOCATOR = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}):(\d+)(?:-(\d+))?")
_PATHLIKE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8}\b|(?<![\w/.-])[\w-]+\.(?:py|md|js|ts|go|rs|"
                       r"java|rb|cs|php|toml|ya?ml|json|cfg|ini)\b")
_BACKTICK = re.compile(r"`([^`]+)`")
_TOKEN = re.compile(r"[A-Za-z_\u00c0-\u024f][\w\u00c0-\u024f]*(?:[.-][A-Za-z0-9_]+)*|\d+(?:\.\d+)?")
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")


@dataclass
class Grade:
    grade: str
    reason: str
    code: str = ""  # machine-readable outcome (call-site codes, see _py_call_grade)

    def __iter__(self):  # (grade, reason) unpacking
        yield self.grade
        yield self.reason


def at_least(grade: str, floor: str) -> bool:
    return GRADES.index(grade) >= GRADES.index(floor)


def cap(grade: str, ceiling: str) -> str:
    return grade if GRADES.index(grade) <= GRADES.index(ceiling) else ceiling


# =============================================================================
# term coverage (general claims, attribution of documents)
# =============================================================================

def _stem(w: str) -> str:
    w = fold_tr(w)
    for suf, rep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", ""), ("ly", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and not w.endswith("ss"):
            return w[: -len(suf)] + rep
    return w


def _num(t: str) -> float | None:
    try:
        return float(t)
    except ValueError:
        return None


def _is_key(tok: str) -> bool:
    """Identifier-like or proper-noun-like tokens that must be matched, not just stemmed."""
    if _num(tok) is not None:
        return True
    if "_" in tok or "." in tok.strip(".") or "()" in tok:
        return True
    if any(ch.isdigit() for ch in tok) and any(ch.isalpha() for ch in tok):
        return True
    inner = tok[1:]
    return any(ch.isupper() for ch in inner) and any(ch.islower() for ch in tok)  # CamelCase / PostgreSQL / SQLite


@dataclass
class Terms:
    keys: list[str]          # folded identifiers / numbers / names
    words: list[str]         # folded, stemmed content words
    locators: list[tuple[str, int, int]]
    paths: list[str]
    negated: bool
    quantified: bool


def claim_terms(text: str) -> Terms:
    text = text or ""
    locators = [(m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))) for m in _LOCATOR.finditer(text)]
    stripped = _LOCATOR.sub(" ", text)
    paths = [m.group(0) for m in _PATHLIKE.finditer(stripped)]
    stripped = _PATHLIKE.sub(" ", stripped)
    raw_lower = {fold_tr(w.strip("'\u2019")) for w in re.findall(r"[\w'\u2019]+", stripped)}
    negated = bool(raw_lower & NEGATIONS) or "n't" in stripped.lower()
    quantified = bool(raw_lower & QUANTIFIERS)
    keys: list[str] = []
    words: list[str] = []
    for bt in _BACKTICK.findall(stripped):
        tok = bt.strip().lstrip(".")
        tok = tok[:-2] if tok.endswith("()") else tok
        if re.fullmatch(r"[\w.]+", tok or "") and not re.fullmatch(r"[\d.]+", tok):
            keys.append(fold_tr(tok))  # a code name as written: matched whole
    stripped = _BACKTICK.sub(lambda m: " " if re.fullmatch(r"\.?[\w.]+(\(\))?", m.group(1).strip()) else
                             " " + m.group(1) + " ", stripped)
    for tok in _TOKEN.findall(stripped.replace("()", " ")):
        tok = tok.strip(".-")
        if not tok:
            continue
        low = fold_tr(tok)
        if _is_key(tok):
            keys.append(low)
            continue
        for part in re.split(r"[-.]", low):
            if not part or part in EN_STOPWORDS or part in TR_STOPWORDS or part in LIGHT:
                continue
            if part in NEGATIONS or part in QUANTIFIERS:
                continue
            if len(part) < 3 and part not in SHORT_TECH:
                continue
            words.append(_stem(part))
    return Terms(list(dict.fromkeys(keys)), list(dict.fromkeys(words)), locators, paths, negated, quantified)


def _vocab(text: str) -> tuple[set[str], set[str], set[float]]:
    """(identifiers folded whole, stemmed word parts, numbers) of an evidence text."""
    idents: set[str] = set()
    parts: set[str] = set()
    nums: set[float] = set()
    for tok in re.findall(r"[A-Za-z_\u00c0-\u024f][\w\u00c0-\u024f]*(?:\.[A-Za-z_]\w*)*|\d+(?:\.\d+)?", text or ""):
        n = _num(tok)
        if n is not None:
            nums.add(n)
            continue
        low = fold_tr(tok)
        idents.add(low)
        for seg in low.split("."):
            idents.add(seg)
        for p in split_identifier(tok):
            parts.add(_stem(p))
            parts.add(p)
    for word in re.findall(r"[A-Za-z\u00c0-\u024f]+", text or ""):
        parts.add(_stem(word))
        parts.add(fold_tr(word))
    return idents, parts, nums


# Common code abbreviations (DESIGN D22): a claim word matches its expansion and back.
ABBREV = {
    "env": ("environ", "environment"), "cfg": ("config", "configuration"), "conf": ("config",),
    "db": ("database", "sqlite", "sql"), "repo": ("repository",), "impl": ("implementation", "implement"),
    "auth": ("authentication", "authorization", "authenticate"), "msg": ("message",), "req": ("request",),
    "resp": ("response",), "arg": ("argument",), "args": ("arguments",), "param": ("parameter",),
    "init": ("initialize", "initialise", "initialization"), "str": ("string",), "dir": ("directory",),
    "pkg": ("package",), "fn": ("function",), "func": ("function",), "var": ("variable",),
    "max": ("maximum",), "min": ("minimum",), "num": ("number",),
}
_ABBREV_BACK = {full: short for short, fulls in ABBREV.items() for full in fulls}


def _word_hit(w: str, parts: set[str]) -> bool:
    if w in parts:
        return True
    for alt in (*ABBREV.get(w, ()), _ABBREV_BACK.get(w, "")):
        if alt and (alt in parts or _stem(alt) in parts):
            return True
    for p in parts:
        short, long_ = (w, p) if len(w) <= len(p) else (p, w)
        if len(short) >= 4 and long_.startswith(short):
            return True
    return False


def _key_hit(k: str, idents: set[str], parts: set[str], nums: set[float]) -> bool:
    n = _num(k)
    if n is not None:
        return n in nums
    if k in idents:
        return True
    if "." in k:
        segs = [s for s in k.split(".") if s]
        return bool(segs) and all(s in idents for s in segs)
    sub = [p for p in re.split(r"[-\s]+", k) if p]
    if len(sub) > 1:  # AES-256, e-mail
        return all(_key_hit(p, idents, parts, nums) for p in sub)
    # SQLite vs sqlite3: an identifier that extends the name
    return len(k) >= 4 and any(i.startswith(k) and (len(i) - len(k)) <= 2 and i[len(k):].isdigit() for i in idents)


def coverage(terms: Terms, text: str) -> dict:
    idents, parts, nums = _vocab(text)
    keys_hit = [k for k in terms.keys if _key_hit(k, idents, parts, nums)]
    words_hit = [w for w in terms.words if _word_hit(w, parts)]
    return {"keys": terms.keys, "keys_hit": keys_hit, "words": terms.words, "words_hit": words_hit,
            "missing": [k for k in terms.keys if k not in keys_hit] + [w for w in terms.words if w not in words_hit]}


def _coverage_grade(terms: Terms, text: str, *, locator_ok: bool = True, strict_polarity_text: str | None = None
                    ) -> Grade:
    if not terms.keys and not terms.words:
        return Grade("none", "the claim has no checkable terms")
    cov = coverage(terms, text)
    complete = not cov["missing"]
    if complete:
        if not locator_ok:
            return Grade("partial", "terms covered, but the claim cites another location (heuristic: term coverage)",
                         "coverage")
        if terms.negated or terms.quantified:
            return Grade("partial", "terms covered, but a negation/quantifier cannot be established by text "
                                    "(heuristic: term coverage)", "coverage")
        # word overlap never verifies: order, direction, conditions and roles are invisible to it
        return Grade("partial", "every term of the claim appears in the evidence, but term coverage does not check "
                                "order, direction, conditions or roles (heuristic: term coverage)", "coverage")
    relevant = bool(cov["keys_hit"]) or len(cov["words_hit"]) >= 2 or \
        (not terms.keys and cov["words_hit"] and 2 * len(cov["words_hit"]) >= len(cov["words"]))
    if relevant:
        return Grade("partial", "evidence is about the claim's subject; missing: " + ", ".join(cov["missing"][:6]),
                     "coverage")
    return Grade("none", "evidence shares no key term with the claim"
                 + (f" (missing: {', '.join(cov['missing'][:6])})" if cov["missing"] else ""))


# =============================================================================
# roles in written text: who calls whom, what a setting is bound to
# =============================================================================

FILE_EXTS = frozenset("""py pyi js mjs cjs ts tsx jsx go rs java kt kts rb php cs md rst txt json toml yaml yml cfg ini
xml html css sql sh c h cc cpp hpp lua scala swift gradle properties""".split())
_CODE_NAME = re.compile(r"`([^`]+)`|(?<![\w./`$-])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\(\))?)")


def is_code_shaped(tok: str) -> bool:
    """A token written as code: snake_case, camelCase/CamelCase, dotted, or ``name()``."""
    t = (tok or "").strip().strip("`")
    if t.endswith("()"):
        return True
    return "." in t.strip(".") or "_" in t.strip("_") or t.startswith("_") or bool(re.search(r"[a-z0-9][A-Z]", t))


def code_names(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, name)`` of the code-shaped names in ``text``, in order (backticked ones too)."""
    out: list[tuple[int, int, str]] = []
    for m in _CODE_NAME.finditer(text or ""):
        if m.group(1) is not None:
            name = m.group(1).strip()
            if not re.fullmatch(r"\.?[\w$]+(?:\.[\w$]+)*(?:\(\))?", name):
                continue  # backticked prose or code, not a name
        else:
            name = m.group(2)
            parts = name.rstrip("()").split(".")
            if len(parts) > 1 and parts[-1].lower() in FILE_EXTS:
                continue  # a file name
            if not is_code_shaped(name) or all(len(p) <= 1 for p in parts):
                continue  # a plain word, or "e.g"
        name = name[:-2] if name.endswith("()") else name
        out.append((m.start(), m.end(), name.lstrip(".")))
    return out


# Relation verbs a written sentence may use. Active English ("A calls B"), passive English with the
# caller after by/from/in ("B is called by A"), Turkish active (verb last, the callee in the
# accusative: "A, B'yi çağırır") and the Turkish agent ("B, A tarafından çağrılır").
_REL_VERB = re.compile(r"\b(?:calls|call|calling|invokes|invoke|invoking|delegates\s+to|delegate\s+to)\b", re.I)
# weaker relation verbs, read only when a clause has no call verb ("A calls B using C" is about calling)
_REL_VERB2 = re.compile(r"\b(?:uses|use|using|imports|import|inherits|inherit|extends|extend|instantiates|instantiate|"
                        r"constructs|construct|creates|create)\b", re.I)
_PASSIVE = re.compile(r"\b(?:called|invoked|used|imported|instantiated|constructed|created)\s+"
                      r"(?:by|from|in|inside|within)\b", re.I)
_TR_VERB = re.compile(r"\b(?:çağırır|çağırıyor|çağırmaktadır|çağırmaz|çağırmıyor|kullanır|kullanıyor|kullanmaktadır|"
                      r"kullanmaz|kullanmıyor)\b", re.I)
_TR_AGENT = re.compile(r"\btarafından\b", re.I)
_CLAUSE_BREAK = re.compile(r";|\.\s|,\s*(?:and|but|while|whereas|so|ama|fakat|ancak)\b|\b(?:while|whereas|but)\b",
                           re.I)
# Turkish case endings right after a name: accusative (the callee) and ablative (order: "B'den önce")
_TR_ACC = re.compile(r"`?['’](?:y?[ıiuü]|n[ıiuü])(?![\wçğıöşü])|`?\s+(?:fonksiyon|metod|metot|yöntem|sınıf|modül)"
                     r"[uüıi]n[uüıi]\b", re.I)
_TR_ABL = re.compile(r"`?['’]n?[dt][ae]n(?![\wçğıöşü])|`?\s+(?:fonksiyon|metod|metot|yöntem|sınıf|modül)\w*[dt][ae]n\b",
                     re.I)
_TR_COORD = re.compile(r"^`?\s*(?:ve|ile|veya|ya da)\s*`?$", re.I)


def _names_and_target(text: str, target: str | None) -> list[tuple[int, int, str]]:
    names = code_names(text)
    tok = _token(target) if target else ""
    if tok:  # a plain-word target (`save`) counts where the text writes it
        for m in re.finditer(rf"(?<![\w.`]){re.escape(tok)}(?![\w`])", text):
            if not any(a <= m.start() < b for a, b, _ in names):
                names.append((m.start(), m.end(), tok))
        names.sort()
    return names


def _clauses(text: str) -> list[tuple[int, int]]:
    out, pos = [], 0
    for m in _CLAUSE_BREAK.finditer(text):
        out.append((pos, m.start()))
        pos = m.end()
    return [*out, (pos, len(text))]


# Words that are never a caller's name (articles, pronouns, adverbs, quantifiers).
_NOT_A_NAME = frozenset(EN_STOPWORDS | TR_STOPWORDS | NEGATIONS | QUANTIFIERS | frozenset(
    "also then just first directly indirectly itself they he she we you i one someone something code each both "
    "either this that it its".split()))
_COORD_WORD = re.compile(r"(?:\band|\bor|\bve|\bveya|\bile|,|&)\s*$", re.I)
# The text between two names of one list ("B, C and D", TR "B ve C'yi"): a coordinator only. A name
# after "instead of", "via", "with the result of", ", which uses" is not one of the list.
_LIST_GAP = re.compile(r"`?(?:['’][^\W\d_]*)?`?\s*(?:,\s*(?:(?:and|or)\s+)?(?:also\s+)?|(?:and|or|ve|veya|ile|ya\s+da|"
                       r"as\s+well\s+as)\s+(?:also\s+)?|&\s*)`?", re.I)


def _listed_with(text: str, names: list[tuple[int, int, str]], i: int) -> list[str]:
    """The names of the list ``names[i]`` belongs to: its neighbours joined to it by coordinators only."""
    lo = hi = i
    while lo > 0 and _LIST_GAP.fullmatch(text[names[lo - 1][1]:names[lo][0]]):
        lo -= 1
    while hi + 1 < len(names) and _LIST_GAP.fullmatch(text[names[hi][1]:names[hi + 1][0]]):
        hi += 1
    return [n[2] for n in names[lo:hi + 1]]


def _plain_word(text: str, a: int, b: int, *, last: bool) -> tuple[int, str] | None:
    """The one plain word (``checkout``) between a and b on the caller's side: the last one before a
    verb (``last``) or the first one after "by"; None when it is a function word or one of a list."""
    words = list(re.finditer(r"[^\W\d][\w]*", text[a:b]))
    if not words:
        return None
    m = words[-1] if last else words[0]
    w = m.group(0)
    if fold_tr(w) in _NOT_A_NAME or len(w) < 2:
        return None
    lo, hi = a + m.start(), a + m.end()
    if _COORD_WORD.search(text[a:lo]) or re.match(r"\s*(?:,|\band\b|\bor\b|\bve\b|\bile\b)", text[hi:b], re.I):
        return None  # "checkout and quote call ..."
    return lo, w


def relation_parse(text: str, target: str | None = None) -> dict | None:
    """The caller and the callee a relation claim's text states, when it states them in one clear form.

    ``{"caller", "caller_file", "callee", "reversed", "callees"}``: ``caller`` is the one name on the
    calling side (``caller_file`` a path there instead), ``callee`` the name on the called side
    (``target`` when the text puts it there), ``callees`` the list the called name belongs to ("A calls
    B and C", "B, C ve D'yi"; a name after "instead of", "via" or ", which uses" is not in it),
    ``reversed`` when the text puts ``target`` on the calling side. The forms: "A
    calls B", "B is called by/from/in A", "A, B'yi çağırır" (the callee in the accusative) and "B, A
    tarafından çağrılır". None when the text does not say it in one of them: no relation verb, several
    clauses and no target to choose one, several names on the calling side ("both A and B call C",
    "B is what A calls"), a relative clause ("B'yi çağıran A") - a guess would bind the wrong roles.
    """
    text = text or ""
    names = _names_and_target(text, target)
    tok = _token(target) if target else ""
    taken = [(a, b) for a, b, _ in names]

    def markers(a: int, b: int) -> list[tuple[str, re.Match]]:
        out: list[tuple[str, re.Match]] = []
        for kind, rx in (("passive", _PASSIVE), ("tr_agent", _TR_AGENT), ("tr_active", _TR_VERB), ("active", _REL_VERB),
                         ("active", _REL_VERB2)):
            if rx is _REL_VERB2 and out:
                break
            for m in rx.finditer(text, a, b):
                if any(s <= m.start() < e for s, e in taken) or re.search(r"\bto\s+$", text[a:m.start()]) or \
                        any(m.start() >= o.start() and m.end() <= o.end() for _, o in out):
                    continue  # inside a code name, an infinitive ("to create"), or part of a passive
                out.append((kind, m))
        return out

    marked = [(a, b, mk) for a, b in _clauses(text) if (mk := markers(a, b))]
    if tok:
        marked = [c for c in marked if any(_token(n) == tok and c[0] <= s < c[1] for s, _, n in names)]
    if len(marked) != 1 or len(marked[0][2]) != 1:
        return None
    ca, cb, [(kind, v)] = marked[0]
    clause = [n for n in names if ca <= n[0] and n[1] <= cb]
    before = [n for n in clause if n[1] <= v.start()]
    after = [n for n in clause if n[0] >= v.end()]
    caller, caller_file, caller_word, callees = None, None, None, []
    if kind == "active":
        if len(before) == 1:
            caller = before[0][2]
        elif not before:
            paths = _PATHLIKE.findall(text[ca:v.start()])
            caller_file = paths[0] if len(paths) == 1 else None
            if caller_file is None and (w := _plain_word(text, ca, v.start(), last=True)):
                caller_word = w[1]  # "checkout calls submit": a plain word, checked against the cited lines
        # the called side is the list right after the verb ("B, C and D"), not a name after "instead of"
        callees = _listed_with(text, after, 0) if after else []
    elif kind == "passive":
        caller = after[0][2] if len(after) == 1 else None
        if not after and (w := _plain_word(text, v.end(), cb, last=False)):
            caller_word = w[1]
        callees = _listed_with(text, before, len(before) - 1) if before else []
    elif kind == "tr_agent":  # the name right before "tarafından" calls, unless it is one of a list
        if before and re.fullmatch(r"`?(?:['’]\w*)?\s*", text[before[-1][1]:v.start()]) and \
                not (len(before) >= 2 and _TR_COORD.match(text[before[-2][1]:before[-1][0]])):
            caller = before[-1][2]
        else:  # "save, checkout tarafından çağrılır": one plain word right before it
            words = re.findall(r"[^\W\d][\w]*", text[(before[-1][1] if before else ca):v.start()])
            if len(words) == 1 and fold_tr(words[0]) not in _NOT_A_NAME:
                caller_word = words[0]
        others = [n for n in clause if caller is None or n is not before[-1]]
        pre = [n for n in others if n[1] <= v.start()]
        post = [n for n in others if n[0] >= v.end()]
        callees = _listed_with(text, pre, len(pre) - 1) if pre else (_listed_with(text, post, 0) if post else [])
    else:  # Turkish active, verb last: the accusative names and those joined to them are called
        if after:
            return None
        objects = {i for i, n in enumerate(before) if _TR_ACC.match(text, n[1])}
        for i in range(len(before) - 2, -1, -1):
            if i + 1 in objects and i not in objects and _TR_COORD.match(text[before[i][1]:before[i + 1][0]]):
                objects.add(i)
        subjects = [n[2] for i, n in enumerate(before) if i not in objects]
        caller = subjects[0] if len(subjects) == 1 and objects else None
        if not subjects and objects:  # "checkout, submit'i çağırır": one plain word before the objects
            words = re.findall(r"[^\W\d][\w]*", text[ca:before[min(objects)][0]])
            if len(words) == 1 and fold_tr(words[0]) not in _NOT_A_NAME:
                caller_word = words[0]
        callees = [n[2] for i, n in enumerate(before) if i in objects]
    if caller is None and caller_file is None and caller_word is None:
        return None
    callee, rev = None, False
    if tok:
        callee = next((c for c in callees if _token(c) == tok), None)
        who = caller or caller_word
        if callee is None and who and _token(who) == tok:
            rev = True
            callee = callees[0] if len(callees) == 1 else None
    elif len(callees) == 1:
        callee = callees[0]
    if callee is None and not rev:
        return None
    return {"caller": caller, "caller_file": caller_file, "caller_word": caller_word, "callee": callee,
            "reversed": rev, "callees": callees}


def relation_roles(text: str, target: str | None = None) -> tuple[str | None, str | None]:
    """``(caller, callee)`` as a relation claim's text states them (:func:`relation_parse`), ``(None, None)``
    when it does not state them in one clear form; the caller is None when it is a file."""
    p = relation_parse(text, target)
    return (p["caller"], p["callee"]) if p else (None, None)


# Negation in written text: English words (NEGATIONS, "n't") and Turkish negative verb forms.
_TR_NEG = re.compile(r"\b\w+(?:maz|mez|mıyor|miyor|muyor|müyor|mamakta|memekte|madan|meden)(?:dı|di|du|dü|lar|ler)?\b|"
                     r"\bdeğil\w*|\bhiçbir\b", re.I)


def _prose(text: str) -> str:
    """``text`` without its code names, backticked spans, paths and locators (the words around them)."""
    t = _BACKTICK.sub(" ", text or "")
    t = _LOCATOR.sub(" ", t)
    t = _PATHLIKE.sub(" ", t)
    for a, b, _ in reversed(code_names(t)):
        t = t[:a] + " " + t[b:]
    return t


def negated_text(text: str) -> str | None:
    """The negation word written text uses ("not", "never", "doesn't", TR "çağırmaz", "değil"), or None."""
    prose = _prose(text)
    for w in re.findall(r"[\w'’]+", prose):
        f = fold_tr(w.strip("'’"))
        if f in NEGATIONS or f.endswith("n't") or f.endswith("n’t"):
            return w
    m = _TR_NEG.search(prose)
    return m.group(0) if m else None


_ORDER_CUE = re.compile(r"\b(before|after|then|önce|sonra|ardından)\b", re.I)
_ORDER_FILLER = frozenset("calling invoking running executing it calls call the a an to any using doing making "
                          "running".split())
_ANAPHOR = re.compile(r"\s*(?:that|this|which|these|those|doing\s+so|so)\b", re.I)  # used with match(text, pos)


_ORDER_IN = re.compile(r"\b(?:in|inside|within|by)\s+(?:the\s+)?(?:(?:function|method|body\s+of)\s+)?$", re.I)
_ORDER_IN_TR = re.compile(r"`?(?:['’]n?[dt][ae]|\s+(?:içinde|içerisinde|tarafından|fonksiyonunda|fonksiyonu\s+içinde|"
                          r"metodunda|metodu\s+içinde))(?![\wçğıöşü])", re.I)
_ORDER_CALLS = re.compile(r"`?\s*(?:(?:first|önce)\s+)?(?:calls|invokes)\b", re.I)  # "`F` calls ..."


def _order_function(text: str, names: list[tuple[int, int, str]]) -> str | None:
    """F of an order sentence that does not pass it: the one name the text puts as the place or the
    caller ("in `F`", "by `F`", "`F` içinde", "`F` calls ..."; TR: the one name without a case ending
    before "çağırır"). None when no name or several take that role - the roles are not guessed."""
    placed = {n[2] for n in names if _ORDER_IN.search(text[:n[0]]) or _ORDER_IN_TR.match(text, n[1])}
    if len(placed) == 1:
        return placed.pop()
    if placed:
        return None
    calling = {n[2] for n in names if _ORDER_CALLS.match(text, n[1])}
    if len(calling) == 1:
        return calling.pop()
    if not calling and _TR_VERB.search(text):
        bare = {n[2] for n in names if not re.match(r"`?['’]", text[n[1]:]) and not _TR_ACC.match(text, n[1])}
        if len(bare) == 1:
            return bare.pop()
    return None


def order_proposition(text: str, where: str | None = None) -> str | None:
    """"A before B in F" from written text, when it states the order in one explicit form.

    "`F` calls `A` before `B`", "In F, `B` runs after `A`", "After `A`, F calls `B`", "`A`, and after
    that `B`", "F calls `A`, then `B`", "F, `B`'den önce `A`'yı çağırır", "F, `A`'dan sonra `B`'yi
    çağırır", "F önce `A`'yı, sonra `B`'yi çağırır". ``where`` is F, the function whose body is
    checked; without it F is the name the text places the calls in or makes the caller
    (:func:`_order_function`: "`A` runs before `B` in `F`", "`A` is called before `B` by `F`"),
    never simply the first name. None when F is not clear, the text does not name two calls and
    their order that way, names more order words than one form uses, or is negated ("never calls
    `B` before `A`"): a reversed proposition would contradict a true sentence.
    """
    text = text or ""
    if negated_text(text):
        return None
    names = code_names(text)
    where = (where or "").strip().strip("`").removesuffix("()") if where else (_order_function(text, names) or "")
    rest = [n for n in names if _token(n[2]) != _token(where)]
    cues = [m for m in _ORDER_CUE.finditer(text) if not any(a <= m.start() < b for a, b, _ in names)]
    if not where or len(rest) < 2 or not cues:
        return None

    def gap_ok(a: int, b: int) -> bool:  # only filler words between a cue and the name it takes
        return all(w.lower() in _ORDER_FILLER for w in re.findall(r"[^\W\d_]+", text[a:b]))

    words = [fold_tr(m.group(1)) for m in cues]
    if words == ["once", "sonra"]:  # "önce A'yı, sonra B'yi": first A, then B
        first = [n for n in rest if cues[0].end() <= n[0] < cues[1].start()]
        second = [n for n in rest if n[0] >= cues[1].end()]
        before_first = [n for n in rest if n[1] <= cues[0].start()]
        if len(first) == 1 and second and not (before_first and _TR_ABL.match(text, before_first[-1][1])):
            return f"{first[0][2]} before {second[0][2]} in {where}"
        return None
    if len(cues) != 1:
        return None
    m, word = cues[0], words[0]
    before = [n for n in rest if n[1] <= m.start()]
    after = [n for n in rest if n[0] >= m.end()]
    if word in ("before", "after", "then"):
        if before and after:
            if word == "after" and _ANAPHOR.match(text, m.end()):  # "A, and after that B"
                a, b = before[-1], after[0]
            elif not gap_ok(m.end(), after[0][0]):
                return None
            else:
                a, b = (after[0], before[-1]) if word == "after" else (before[-1], after[0])
        elif not before and len(after) == 2 and word in ("before", "after"):  # "After A, F calls B"
            a, b = (after[0], after[1]) if word == "after" else (after[1], after[0])
        else:
            return None
    else:  # Turkish: "B'den önce A" / "A'dan sonra B"; "A'yı çağırır, sonra B'yi" (then)
        if len(rest) != 2 or not before:
            return None
        ref = before[-1]
        other = next(n for n in rest if n is not ref)
        if _TR_ABL.match(text, ref[1]):
            a, b = (other, ref) if word == "once" else (ref, other)
        elif word in ("sonra", "ardindan") and after:
            a, b = ref, after[0]
        else:
            return None
    return f"{a[2]} before {b[2]} in {where}"


def claimed_caller(spec: dict | None, subjects: list[str] | None, text: str | None) -> str | None:
    """The caller a relation claim states: its first subject's symbol, the spec's ``source_label``, or, for
    written text (``spec.free_text``), the caller the text states in a clear form (:func:`relation_parse`)."""
    spec = spec or {}
    subjects = list(subjects or [])
    _, a_sym = _subject_parts(subjects[0] if subjects else None)
    caller = a_sym or spec.get("source_label")
    if caller:
        if re.search(r"\.[A-Za-z]{1,5}$", caller.strip()) and not caller.strip().endswith(")"):
            return None  # the caller is a module (file node): no enclosing def to check
        return caller
    if spec.get("free_text"):
        p = relation_parse(text or "", spec.get("target_label"))
        if p and p["caller"] and not p["reversed"]:
            return p["caller"]
    return None


def caller_from_text(spec: dict | None, subjects: list[str] | None) -> bool:
    """Is the caller of :func:`claimed_caller` read from written text (not from the claim's subjects)?"""
    spec = spec or {}
    _, a_sym = _subject_parts((list(subjects or []) or [None])[0])
    return bool(spec.get("free_text")) and not (a_sym or spec.get("source_label"))


# Words that describe a setting, not what it sets ("... is read from the environment variable X").
_CONFIG_WORDS = frozenset("""environment environ variable variables env setting settings config configuration
configured configurable value values default defaults come comes coming taken given control controls controlled
set sets option options parameter parameters key keys name""".split())


def config_subject(text: str, var: str) -> list[str]:
    """What a config claim's text says ``var`` sets: its content words and the parts of its code names,
    without the variable's own words and the vocabulary of settings."""
    var_parts = {p for w in split_identifier(var) for p in (w, _stem(w))}
    out: list[str] = []
    for _, _, name in code_names(text):
        if name == var or name.lower() == var.lower():
            continue
        out += [_stem(p) for p in split_identifier(name.replace(".", "_")) if len(p) >= 2]
    for w in claim_terms(_BACKTICK.sub(" ", text or "")).words:
        if w in _CONFIG_WORDS or _stem(w) in {_stem(x) for x in _CONFIG_WORDS}:
            continue
        if split_identifier(w) and all(p in var_parts for p in split_identifier(w)):
            continue
        out.append(w)
    return [w for w in dict.fromkeys(out) if not _word_hit(w, var_parts)]


def binds(names: list[str], subject: list[str]) -> list[str]:
    """The subject words that the binding ``names`` (assignment targets, keys, enclosing definitions) spell."""
    parts: set[str] = set()
    for n in names:
        for p in split_identifier(n):
            parts.update((p, _stem(p)))
    return [w for w in subject if _word_hit(w, parts)]


# =============================================================================
# reading the evidence
# =============================================================================

def _root(repo: Path | None, ev: dict) -> Path | None:
    meta = ev.get("meta") or {}
    if meta.get("root"):
        return Path(meta["root"])
    return Path(repo) if repo is not None else None


def _file_text(repo: Path | None, ev: dict) -> str | None:
    root = _root(repo, ev)
    if root is None or not ev.get("path"):
        return None
    try:
        return (root / ev["path"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _cited_range(repo: Path | None, ev: dict) -> tuple[int, int] | None:
    """Current lines of file evidence (relocated when its anchor moved)."""
    if not ev.get("line_start"):
        return None
    a, b = int(ev["line_start"]), int(ev.get("line_end") or ev["line_start"])
    if _root(repo, ev) is None:
        return a, b
    chk = evmod.check_source(Path(repo) if repo is not None else Path("."), ev)  # meta.root wins inside
    if chk.ok and chk.moved_to:
        return chk.moved_to
    return a, b


def _evidence_text(repo: Path | None, ev: dict) -> tuple[str, int | None, int | None, str | None]:
    """(text, start, end, file text) that the evidence shows."""
    typ = evmod.effective_type(ev)
    if typ in evmod.FILE_TYPES and ev.get("path"):
        full = _file_text(repo, ev)
        rng = _cited_range(repo, ev) if full is not None else None
        if full is not None and rng:
            lines = full.splitlines()
            a, b = rng
            if 0 < a <= len(lines):
                return "\n".join(lines[a - 1: min(b, len(lines))]), a, b, full
        return ev.get("excerpt") or "", ev.get("line_start"), ev.get("line_end"), full
    if typ in ("experiment", "test_result"):
        meta = ev.get("meta") or {}
        extra = " ".join(str(meta.get(k) or "") for k in ("caller_qual", "callee_qual", "hypothesis"))
        tests = meta.get("tests") or []
        extra += " " + " ".join(str(t) for t in (tests if isinstance(tests, list) else [tests])[:10])
        return f"{ev.get('locator') or ''}\n{ev.get('excerpt') or ''}\n{extra}", None, None, None
    if typ == "git_history":
        return f"{ev.get('locator') or ''}\n{ev.get('excerpt') or ''}", None, None, None
    if typ == "graph_edge":
        return ev.get("locator") or "", None, None, None
    return f"{ev.get('excerpt') or ''}\n{ev.get('locator') or ''}", None, None, None


def _context_text(repo: Path | None, ev: dict, start: int | None, end: int | None, full: str | None) -> str:
    """Enclosing symbol name and file name: what the cited lines are part of."""
    out = []
    if ev.get("path"):
        out.append(PurePosixPath(ev["path"]).stem)
    if full is None and ev.get("path") and ev.get("line_start"):  # e.g. a run observed at a call site
        full = _file_text(repo, ev)
        start, end = int(ev["line_start"]), int(ev.get("line_end") or ev["line_start"])
    if full is not None and start and ev.get("path"):
        facts = _facts(ev["path"], full)
        where = anchors.enclosing(facts, start, end or start)
        if where and where[0] == "sym":
            out.append(where[1].split("#")[0])
        elif where and where[0] == "sec":
            out.append(where[1])
    return " ".join(out)


_FACTS: dict[tuple[str, int, int], dict | None] = {}


def _facts(rel: str, text: str) -> dict | None:
    key = (rel, len(text), hash(text))
    if key not in _FACTS:
        if len(_FACTS) > 512:
            _FACTS.clear()
        _FACTS[key] = anchors.compute_facts(rel, text.encode("utf-8", "surrogatepass"))
    return _FACTS[key]


def _py_tree(text: str) -> ast.AST | None:
    return anchors._parse_py(text)


# =============================================================================
# per-kind predicates
# =============================================================================

def _token(label: str | None) -> str:
    return (label or "").rpartition("::")[2].strip().lstrip(".").split("(")[0].rpartition(".")[2].strip()


def _subject_parts(subject: str | None) -> tuple[str | None, str | None]:
    if not subject:
        return None, None
    if "::" in subject:
        path, _, sym = subject.partition("::")
        return path, sym.strip()
    return subject, None


def _module_of(path: str | None) -> str | None:
    if not path or not path.endswith(".py"):
        return None
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _mod_matches(imported: str, target_path: str | None, importer: str | None, level: int = 0) -> bool:
    """Does ``from imported import x`` (or ``import imported``) refer to the module at ``target_path``?"""
    target = _module_of(target_path)
    if target is None:
        return True  # nothing to compare against: do not refute on it
    if level and importer:
        pkg = list(PurePosixPath(importer).parent.parts)
        pkg = pkg[: len(pkg) - (level - 1)] if level > 1 else pkg
        imported = ".".join([*pkg, *([imported] if imported else [])])
    if not imported:
        return False
    return target == imported or target.endswith("." + imported) or imported.endswith("." + target)


def _enclosing_def(tree: ast.AST, line: int) -> list[ast.AST]:
    """Chain of defs (outermost first) containing ``line``."""
    chain: list[ast.AST] = []

    def walk(node: ast.AST) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and \
                    ch.lineno <= line <= (ch.end_lineno or ch.lineno):
                chain.append(ch)
                walk(ch)
                return
            if hasattr(ch, "lineno") and not (getattr(ch, "lineno", 0) <= line <= (getattr(ch, "end_lineno", 0) or 0)):
                continue
            walk(ch)

    walk(tree)
    return chain


def _local_rebinding(fn: ast.AST, name: str, before: int) -> int | None:
    """Line where ``name`` is bound locally in ``fn`` before ``before`` (params count), else None."""
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    args = fn.args
    for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
        if a is not None and a.arg == name:
            return a.lineno
    for node in ast.walk(fn):
        if node is fn or getattr(node, "lineno", before) >= before:
            continue
        if isinstance(node, ast.Global) and name in node.names:
            return None
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store):
            return node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return node.lineno
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if (a.asname or a.name.split(".")[0]) == name:
                    return node.lineno
    return None


def _module_binding(tree: ast.AST, name: str) -> list[tuple[str, ast.AST]]:
    """Module-level statements binding ``name``: [(kind, node)] with kind import|from|def|assign."""
    out: list[tuple[str, ast.AST]] = []

    def scan(stmts: list[ast.stmt]) -> None:
        for s in stmts:
            if isinstance(s, ast.Import):
                for a in s.names:
                    if (a.asname or a.name.split(".")[0]) == name:
                        out.append(("import", s))
            elif isinstance(s, ast.ImportFrom):
                for a in s.names:
                    if (a.asname or a.name) == name or a.name == "*":
                        out.append(("from", s))
            elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if s.name == name:
                    out.append(("def", s))
            elif isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                if any(isinstance(n, ast.Name) and n.id == name for t in targets for n in ast.walk(t)):
                    out.append(("assign", s))
            else:
                for sub in ("body", "orelse", "finalbody"):
                    if isinstance(getattr(s, sub, None), list):
                        scan(getattr(s, sub))
                for h in getattr(s, "handlers", None) or []:
                    scan(h.body)

    scan(tree.body)  # type: ignore[attr-defined]
    return out


def assignment_spans(tree: ast.AST, name: str) -> list[tuple[int, int]]:
    """``(start, end)`` of the module-level and class-level assignments binding ``name`` (``Cls.name``:
    in class ``Cls``) - how a constant or a module variable is defined."""
    owner, _, bare = name.strip().strip("`").split("(")[0].rpartition(".")
    owner = owner.rpartition(".")[2]
    out: list[tuple[int, int]] = []

    def scan(stmts: list[ast.stmt], cls: str | None) -> None:
        for s in stmts:
            if isinstance(s, ast.ClassDef):
                scan(s.body, s.name)
            elif isinstance(s, (ast.Assign, ast.AnnAssign)):
                targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                if (not owner or owner == cls) and \
                        any(isinstance(n, ast.Name) and n.id == bare for t in targets for n in ast.walk(t)):
                    out.append((s.lineno, s.end_lineno or s.lineno))
            elif not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ("body", "orelse", "finalbody"):
                    if isinstance(getattr(s, sub, None), list):
                        scan(getattr(s, sub), cls)
                for h in getattr(s, "handlers", None) or []:
                    scan(h.body, cls)

    scan(tree.body, None)  # type: ignore[attr-defined]
    return out


def binds_name(tree: ast.AST, name: str) -> bool:
    """Does anything in the tree bind ``name``: a def or class, an assignment or loop/with target, an
    attribute store (``self.name = ...``), a parameter, an import (or its alias), ``global``/``nonlocal``,
    an ``except ... as`` or a match capture?"""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            return True
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store):
            return True
        if isinstance(n, ast.Attribute) and n.attr == name and isinstance(n.ctx, ast.Store):
            return True
        if isinstance(n, ast.arg) and n.arg == name:
            return True
        if isinstance(n, ast.alias) and name in (n.asname, n.name, n.name.split(".")[0], n.name.rpartition(".")[2]):
            return True
        if isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
            return True
        if isinstance(n, ast.ExceptHandler) and n.name == name:
            return True
        if type(n).__name__ in ("MatchAs", "MatchStar", "MatchMapping") and \
                name in (getattr(n, "name", None), getattr(n, "rest", None)):
            return True
    return False


def _import_names(tree: ast.AST, token: str) -> dict[str, tuple[str, int, str]]:
    """local name -> (module, level, original) for every import that binds ``token`` or an alias of it."""
    out: dict[str, tuple[str, int, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == token:
                    out[a.asname or a.name] = (node.module or "", node.level, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.rpartition(".")[2] == token and a.asname:
                    out[a.asname] = (a.name.rpartition(".")[0], 0, token)
    return out


def _calls_at(tree: ast.AST, line: int) -> list[ast.Call]:
    """Calls whose callee name sits on ``line`` (the attribute of a method call, the name of a call)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            pos = f.end_lineno if isinstance(f, ast.Attribute) else getattr(f, "lineno", None)
            if pos == line:
                out.append(node)
    return out


def names_called_at(text: str, rel: str, line: int) -> list[str] | None:
    """Local names through which the calls on ``line`` reach their callee.

    ``helper(x)`` gives ``helper`` (an import alias included), ``mod.f()`` gives
    the receiver root ``mod``. None when the file cannot be parsed as Python.
    """
    if not rel.endswith((".py", ".pyi")):
        return None
    tree = _py_tree(text)
    if tree is None:
        return None
    out: list[str] = []
    for call in _calls_at(tree, line):
        f = call.func
        while isinstance(f, (ast.Attribute, ast.Call, ast.Subscript)):
            f = f.value if not isinstance(f, ast.Call) else f.func
        if isinstance(f, ast.Name):
            out.append(f.id)
    return list(dict.fromkeys(out))


# Codes of call-site outcomes (critique maps them to pass / heuristic / definitive findings):
#   call, alias_call, self_call, module_call  -> full
#   method_unresolved                         -> partial (receiver type unknown: an inference)
#   rebound, other_module, module_assign, unbound, not_called, no_grammar -> partial (heuristic doubt)
#   outside_caller, string_only, absent, wrong_module, unreadable, blank  -> none
def _py_call_grade(text: str, rel: str, line: int, token: str, *, caller: str | None,
                   target_path: str | None, target_qual: str | None, relation: str | None) -> Grade:
    lines = text.splitlines()
    src = lines[line - 1] if 0 < line <= len(lines) else ""
    tree = _py_tree(text)
    if tree is None:
        named = bool(re.search(rf"\b{re.escape(token)}\b", src))
        return Grade("partial" if named else "none", "file does not parse; textual match only",
                     "no_grammar" if named else "absent")
    chain = _enclosing_def(tree, line)
    fn = next((d for d in reversed(chain) if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if caller:
        cname = _token(caller)
        names = [d.name for d in chain]
        if cname and cname not in names and cname != PurePosixPath(rel).stem:
            return Grade("none", f"line {line} is not inside `{cname}` (it is in "
                                 f"{'.'.join(names) or 'module level'})", "outside_caller")
        # `Owner.name` written as a class's member: the class must enclose the line too
        owner = caller.strip().lstrip(".").split("(")[0].rpartition(".")[0].rpartition(".")[2]
        if cname in names and owner[:1].isupper() and owner not in names[:names.index(cname)]:
            return Grade("none", f"line {line} is inside `{'.'.join(names)}`, not inside a class `{owner}`",
                         "outside_caller")
    rel_kind = (relation or "calls").lower()
    if rel_kind == "inherits":
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.lineno <= line <= (node.end_lineno or node.lineno):
                for b in node.bases:
                    bn = b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else None
                    if bn == token or bn in _import_names(tree, token):
                        return Grade("full", f"class at line {node.lineno} has base `{bn}`", "call")
        return Grade("none", f"no class at line {line} derives from `{token}`", "absent")
    if rel_kind in ("imports", "imports_from"):
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and \
                    node.lineno <= line <= (node.end_lineno or node.lineno):
                mod = getattr(node, "module", None) or ""
                if any(a.name.rpartition(".")[2] == token or a.name == token for a in node.names) or \
                        mod.rpartition(".")[2] == token:
                    return Grade("full", f"import at line {node.lineno} names `{token}`", "call")
        return Grade("none", f"no import of `{token}` at line {line}", "absent")
    aliases = _import_names(tree, token)
    if rel_kind not in ("calls", "call"):
        # uses / references / method / contains ...: a reference in the syntax tree is what is claimed
        for node in ast.walk(tree):
            if getattr(node, "lineno", None) != line and not (
                    isinstance(node, ast.Attribute) and getattr(node, "end_lineno", None) == line):
                continue
            if (isinstance(node, ast.Name) and (node.id == token or node.id in aliases)) or \
                    (isinstance(node, ast.Attribute) and node.attr == token) or \
                    (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == token):
                return Grade("full", f"line {line} references `{token}` in the syntax tree", "reference")
        if token and token in src:
            return Grade("none", f"`{token}` appears at line {line} only in a string or comment", "string_only")
        return Grade("none", f"line {line} does not reference `{token}`", "absent")
    for call in _calls_at(tree, line):
        f = call.func
        if isinstance(f, ast.Name) and (f.id == token or f.id in aliases):
            if fn is not None:
                rb = _local_rebinding(fn, f.id, line)
                if rb is not None:
                    return Grade("partial", f"`{f.id}` is rebound locally at line {rb} before the call", "rebound")
            if f.id in aliases:
                mod, level, _ = aliases[f.id]
                if _mod_matches(mod, target_path, rel, level):
                    if f.id != token:
                        return Grade("full", f"AST call to `{f.id}` (import alias of `{token}`) at line {line}",
                                     "alias_call")
                    return Grade("full", f"AST call to `{token}` at line {line}, imported from the target's module",
                                 "call")
                return Grade("partial", f"`{f.id}` is imported from `{'.' * level}{mod}`, not from the target's "
                                        "module", "other_module")
            binds = _module_binding(tree, token)
            kinds = {k for k, _ in binds}
            if "def" in kinds and (target_path is None or target_path == rel):
                if "assign" in kinds:
                    return Grade("partial", f"`{token}` is also assigned at module level; the call may not reach "
                                            "the def", "module_assign")
                return Grade("full", f"AST call to `{token}` at line {line}, defined in this file", "call")
            if "from" in kinds:
                # `from m import orig as token`: the claim names the alias; its source is that import
                alias = next(((n, a) for k, n in binds if k == "from" for a in n.names  # type: ignore[attr-defined]
                              if a.asname == token and a.name != "*"), None)
                if alias is not None:
                    n, a = alias
                    src_mod = f"{'.' * (n.level or 0)}{n.module or ''}"  # type: ignore[attr-defined]
                    if _mod_matches(n.module or "", target_path, rel, n.level or 0):  # type: ignore[attr-defined]
                        return Grade("full", f"AST call to `{token}` at line {line}, an import alias of `{a.name}` "
                                             f"from `{src_mod}`", "alias_call")
                    return Grade("partial", f"`{token}` is an import alias of `{a.name}` from `{src_mod}`, not from "
                                            "the target's module", "other_module")
                return Grade("partial", f"`{token}` at line {line} is star-imported; its source is not resolved",
                             "unbound")
            if "assign" in kinds:
                return Grade("partial", f"`{token}` is assigned at module level; the call may not reach the def",
                             "module_assign")
            if target_path is None:
                return Grade("full", f"AST call to `{token}` at line {line}", "call")
            return Grade("partial", f"AST call to `{token}` at line {line}, but nothing in {rel} binds it to "
                                    f"{target_path}", "unbound")
        if isinstance(f, ast.Attribute) and f.attr == token:
            recv = f.value
            written = (target_qual or "").strip().strip("`").split("(")[0].strip(".")
            if "." in written and written.rpartition(".")[2] == token and \
                    ast.unparse(recv) == written.rpartition(".")[0]:
                return Grade("full", f"AST call `{written}()` at line {line}, on the receiver the claim writes",
                             "receiver_call")
            if isinstance(recv, ast.Name):
                binds = _module_binding(tree, recv.id)
                mods = [(a.name, n) for k, n in binds if k == "import" for a in n.names  # type: ignore[attr-defined]
                        if (a.asname or a.name.split(".")[0]) == recv.id]
                frm = [(f"{n.module}.{a.name}" if n.module else a.name, n) for k, n in binds if k == "from"
                       for a in n.names if (a.asname or a.name) == recv.id]  # type: ignore[attr-defined]
                if mods or frm:
                    for modname, n in mods + frm:
                        tp = target_path
                        if tp is None and target_qual and "." in target_qual.strip("."):
                            prefix = target_qual.strip().strip(".").split("(")[0].rpartition(".")[0]
                            if prefix and (modname == prefix or modname.endswith("." + prefix)
                                           or prefix.endswith(modname)):
                                return Grade("full", f"AST call `{recv.id}.{token}` on module `{modname}`",
                                             "module_call")
                        if tp is not None and _mod_matches(modname, tp, rel, getattr(n, "level", 0) or 0):
                            return Grade("full", f"AST call `{recv.id}.{token}` on the target's module", "module_call")
                    if target_path:
                        return Grade("none", f"`{recv.id}.{token}` calls into another module", "wrong_module")
                if recv.id in ("self", "cls"):
                    cls = next((d for d in reversed(chain) if isinstance(d, ast.ClassDef)), None)
                    owner = (target_qual or "").split("#")[0].strip(".").split("(")[0].rpartition(".")[0]
                    if cls is not None and owner and owner.rpartition(".")[2] == cls.name \
                            and target_path in (None, rel):
                        return Grade("full", f"`{recv.id}.{token}()` inside class `{cls.name}`", "self_call")
                    if cls is not None and not owner and target_path in (None, rel) and \
                            any(isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == token
                                for s in cls.body):
                        return Grade("full", f"`{recv.id}.{token}()` inside class `{cls.name}`, which defines "
                                             f"`{token}`", "self_call")
            return Grade("partial", f"method call `.{token}()` at line {line}; the receiver's type is not "
                                    "resolved statically", "method_unresolved")
    code_only = _strip_strings_comments(src)
    if re.search(rf"(?<![\w.]){re.escape(token)}\b|\.{re.escape(token)}\b", code_only) or \
            any(re.search(rf"(?<![\w.]){re.escape(a)}\b", code_only) for a in aliases if a != token):
        return Grade("partial", f"line {line} names `{token}` but has no call to it (passed as a value?)",
                     "not_called")
    if token and token in src:
        return Grade("none", f"`{token}` appears at line {line} only in a string or comment", "string_only")
    return Grade("none", f"line {line} does not name `{token}`", "absent")


def _strip_strings_comments(src: str) -> str:
    s = re.sub(r"(\"[^\"]*\"|'[^']*')", "\"\"", src)
    return re.sub(r"#.*$", "", s)


_TS_CALL_TYPES = ("call_expression", "call", "method_invocation", "invocation_expression", "function_call",
                  "new_expression", "object_creation_expression", "method_call_expression")


_JAVA_PACKAGE = re.compile(r"[a-z_][\w]*(?:\.[a-z_][\w]*)*")


def _java_qualified_grade(lines: list[str], rel: str, line: int, token: str, qualified: str,
                          target_path: str) -> Grade:
    """``Cls.token(...)`` (``qualified`` = ``Cls`` or ``pkg.Cls``): is ``Cls`` the target's class?

    Yes when the qualifier is the target's package, or ``Cls`` is imported from it (explicitly or
    with ``pkg.*``), or the caller is in the same package. An import of a same-named class from
    another package means another class; a qualifier that is not a package (``Outer.Cls``, a
    field) is left unresolved.
    """
    prefix, _, cls = qualified.rpartition(".")
    pkg_dir = PurePosixPath(target_path).parent.as_posix()

    def names_target_pkg(pkg: str) -> bool:
        p = pkg.replace(".", "/")
        return bool(p) and (pkg_dir == p or pkg_dir.endswith("/" + p))

    call = f"`{qualified}.{token}` at line {line}"
    if prefix:
        if not _JAVA_PACKAGE.fullmatch(prefix) or prefix.split(".")[0] in ("this", "super"):
            return Grade("partial", f"call {call}; `{prefix}` is not a package name, so `{cls}` is not resolved",
                         "method_unresolved")
        if names_target_pkg(prefix):
            return Grade("full", f"syntax-tree call {call} (fully qualified)", "call")
        return Grade("none", f"call {call} names another package's `{cls}`", "wrong_module")
    explicit = [m.group(2) for ln in lines if (m := re.match(rf"\s*import\s+(static\s+)?([\w.]+)\.{re.escape(cls)}\s*;", ln))]
    if explicit:
        if any(names_target_pkg(p) for p in explicit):
            return Grade("full", f"syntax-tree call {call} (imported)", "call")
        return Grade("none", f"call {call}; `{cls}` is imported from {explicit[0]}, not from the target's package",
                     "wrong_module")
    declared = next((m.group(1) for ln in lines if (m := re.match(r"\s*package\s+([\w.]+)\s*;", ln))), None)
    same_pkg = PurePosixPath(rel).parent.as_posix() == pkg_dir or (declared is not None and names_target_pkg(declared))
    if same_pkg:
        return Grade("full", f"syntax-tree call {call} (same package)", "call")
    wildcard = [m.group(1) for ln in lines if (m := re.match(r"\s*import\s+([\w.]+)\.\*\s*;", ln))]
    if any(names_target_pkg(p) for p in wildcard):
        return Grade("full", f"syntax-tree call {call} (imported with *)", "call")
    return Grade("partial", f"call {call}; `{cls}` is not imported in this file", "unbound")


def _other_call_grade(text: str, rel: str, line: int, token: str, *, target_path: str | None) -> Grade:
    lines = text.splitlines()
    src = lines[line - 1] if 0 < line <= len(lines) else ""
    parser = anchors._ts_parser(PurePosixPath(rel).suffix.lower())
    direct = member = False
    if parser is None:
        if re.search(rf"(?<![\w.$]){re.escape(token)}\s*\(", src):
            return Grade("partial", f"line {line} looks like a call to `{token}` (no grammar: textual)", "no_grammar")
        return Grade("none", f"line {line} does not name `{token}`", "absent")
    tree = parser.parse(text.encode("utf-8", "surrogatepass"))
    target_cls = PurePosixPath(target_path).stem if target_path else None
    qualified: str | None = None
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.start_point[0] > line - 1 or n.end_point[0] < line - 1:
            continue
        if n.type in _TS_CALL_TYPES:
            fn = n.child_by_field_name("function") or n.child_by_field_name("name") or \
                n.child_by_field_name("method") or n.child_by_field_name("constructor")
            ft = fn.text.decode("utf-8", "replace") if fn is not None else ""
            if fn is not None and fn.end_point[0] == line - 1:
                obj = n.child_by_field_name("object") if n.type == "method_invocation" else None
                ot = obj.text.decode("utf-8", "replace") if obj is not None else ""
                if ft == token and obj is not None and ot not in ("this", "super"):
                    # Java `Wisp.spawn(...)`: a class qualifier names the target's class;
                    # any other receiver (`npc.spawn()`) is a method call on an unresolved type
                    if target_cls and ot.rpartition(".")[2] == target_cls:
                        qualified = ot
                    else:
                        member = True
                elif ft == token:
                    direct = True
                elif re.search(rf"[.:>]{re.escape(token)}$", ft):
                    member = True
        stack.extend(n.children)
    if qualified:
        return _java_qualified_grade(lines, rel, line, token, qualified, target_path)
    if direct:
        same_file = target_path in (None, rel)
        imported = any(token in ln and re.search(r"\b(import|require|use|include)\b", ln) for ln in lines)
        if same_file or imported:
            return Grade("full", f"syntax-tree call to `{token}` at line {line}", "call")
        return Grade("partial", f"call to `{token}` at line {line}; no import in this file names it", "unbound")
    if member:
        return Grade("partial", f"method call `.{token}()` at line {line}; receiver type not resolved",
                     "method_unresolved")
    if re.search(rf"\b{re.escape(token)}\b", src):
        return Grade("partial", f"line {line} names `{token}` but has no call to it", "not_called")
    return Grade("none", f"line {line} does not name `{token}`", "absent")


def call_site(repo: Path | str | None, path: str, line: int, target_label: str, *, caller: str | None = None,
              target_path: str | None = None, target_qual: str | None = None,
              relation: str | None = None) -> Grade:
    """Grade one call site ``path:line`` for a call to ``target_label`` (``Grade.code`` says what was found)."""
    root = Path(repo) if repo is not None else None
    token = _token(target_label)
    if root is None:
        return Grade("none", "no repository to read", "unreadable")
    try:
        text = (root / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Grade("none", f"{path} cannot be read", "unreadable")
    lines = text.splitlines()
    if not 0 < line <= len(lines):
        return Grade("none", f"line {line} does not exist in {path}", "unreadable")
    if not lines[line - 1].strip():
        return Grade("none", f"{path}:{line} is blank", "blank")
    if path.endswith((".py", ".pyi")):
        return _py_call_grade(text, path, line, token, caller=caller, target_path=target_path,
                              target_qual=target_qual, relation=relation)
    return _other_call_grade(text, path, line, token, target_path=target_path)


def _defs_named(tree: ast.AST, name: str) -> list[ast.AST]:
    """Definitions called ``name`` (``Cls.name``: methods of class ``Cls``; a class counts with its whole
    body), in source order."""
    owner, _, bare = name.strip().strip("`").rstrip("()").rpartition(".")
    owner = owner.rpartition(".")[2]
    out = []

    def walk(node: ast.AST, cls: str | None) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and ch.name == bare and \
                    (not owner or owner == cls):
                out.append(ch)
            walk(ch, ch.name if isinstance(ch, ast.ClassDef) else cls)

    walk(tree, None)
    return out


# Code inside these runs when it is called, not where it stands in the enclosing body.
_INNER_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def calls_in(tree: ast.AST, fn: ast.AST, token: str, *, nested: bool | None = None) -> list[int]:
    """Lines in ``fn``'s whole body with a call whose callee is named ``token``: ``token(...)``, an import
    alias of it, or ``x.token(...)`` on any receiver (a method call counts: its receiver is not resolved).

    ``nested``: None counts every call; False only the calls in ``fn``'s own code; True only those
    inside a nested def, lambda or class (their order against ``fn``'s own calls is not static)."""
    aliases = set(_import_names(tree, token)) | {token}
    out = []

    def visit(node: ast.AST, inner: bool) -> None:
        for ch in ast.iter_child_nodes(node):
            deeper = inner or isinstance(ch, _INNER_SCOPES)
            if isinstance(ch, ast.Call) and (nested is None or nested == deeper):
                f = ch.func
                if (isinstance(f, ast.Name) and f.id in aliases) or (isinstance(f, ast.Attribute) and f.attr == token):
                    out.append(f.end_lineno if isinstance(f, ast.Attribute) else ch.lineno)
            visit(ch, deeper)

    visit(fn, False)
    return sorted(set(out))


def caller_scope(repo, caller: str, token: str, *, path: str | None = None, files: list[str] | None = None) -> dict | None:
    """Direct calls to ``token`` in the whole body of every Python definition named ``caller``.

    Looks in ``path`` first, then in ``files`` (other files that define the caller, e.g. from the graph).
    ``{"defs": [(file, name, start, end)], "calls": [(file, line)], "scope": "..."}``, or None when
    no definition of the caller is found or a file does not parse - then nothing can be said about the
    whole body. Calls through other names (a variable holding the function, ``getattr``, dynamic
    dispatch) are not followed; the scope text says so.
    """
    if repo is None or not caller or not token:
        return None
    defs: list[tuple[str, ast.AST, ast.AST]] = []
    for rel in dict.fromkeys([p for p in [path, *(files or [])] if p]):
        if not rel.endswith((".py", ".pyi")):
            continue
        try:
            text = (Path(repo) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tree = _py_tree(text)
        if tree is None:
            return None
        defs += [(rel, tree, d) for d in _defs_named(tree, caller)]
        if defs and rel == path:
            break  # the cited file defines it: that is the caller the claim cites
    if not defs:
        return None
    calls = [(rel, ln) for rel, tree, d in defs for ln in calls_in(tree, d, token)]
    where = ", ".join(f"{rel}:{d.lineno}-{d.end_lineno or d.lineno}" for rel, _, d in defs[:3])
    name = caller.strip().rstrip("()")
    scope = (f"{name} ({where})" if len(defs) == 1 else f"the {len(defs)} definitions named {name} ({where})")
    return {"defs": [(rel, d.name, d.lineno, d.end_lineno or d.lineno) for rel, _, d in defs], "calls": calls,
            "scope": scope,
            "miss": f"no direct call to {token} in {scope}; calls through other names are not followed"}


_ORDER_PROP = re.compile(r"^\s*`?([\w.]+?)(?:\(\))?`?\s+before\s+`?([\w.]+?)(?:\(\))?`?\s+in\s+`?([\w./:]+?)(?:\(\))?`?\s*$",
                         re.I)


def call_order(text: str, rel: str, where: str, first: str, second: str, *,
               near: tuple[int, int] | None = None) -> dict | None:
    """Where the first calls of ``first`` and ``second`` are in the whole body of the definition ``where``.

    ``{"name", "start", "end", "lines": {name: first call line}, "nested": {name: first line}, "branchy"}``:
    ``lines`` holds the calls in F's own code (a name with none is missing), ``nested`` the calls
    inside a def, lambda or class nested in F, which run when that is called - their place in F's
    order is not known statically. None when the file is not Python or defines no ``where``. With
    several definitions of that name, the one overlapping ``near`` (the cited lines) is used.
    """
    if not rel.endswith((".py", ".pyi")):
        return None
    tree = _py_tree(text)
    if tree is None:
        return None
    fns = _defs_named(tree, where.rpartition("::")[2])
    if near:
        fns = sorted(fns, key=lambda d: not (d.lineno <= near[1] and near[0] <= (d.end_lineno or d.lineno)))
    if not fns:
        return None
    fn = fns[0]
    lines, nested = {}, {}
    for want in (first, second):
        tok = want.rpartition(".")[2]
        found = calls_in(tree, fn, tok, nested=False)
        if found:
            lines[want] = found[0]
        inner = calls_in(tree, fn, tok, nested=True)
        if inner:
            nested[want] = inner[0]
    branchy = any(isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.AsyncFor, ast.AsyncWith))
                  for n in ast.walk(fn) if n is not fn)
    return {"name": fn.name, "start": fn.lineno, "end": fn.end_lineno or fn.lineno, "lines": lines,
            "nested": nested, "branchy": branchy}


def def_around(repo, path: str, a: int, b: int, name: str) -> bool | None:
    """Does a Python definition named ``name`` overlap lines a-b of ``path``? None when the file is not
    Python, cannot be read or does not parse (nothing can be said)."""
    if repo is None or not path.endswith((".py", ".pyi")):
        return None
    try:
        tree = _py_tree((Path(repo) / path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if tree is None:
        return None
    return any(d.lineno <= b and a <= (d.end_lineno or d.lineno) for d in _defs_named(tree, name))


def order_check(repo, ev: dict, proposition: str, holds: bool = True) -> Grade | None:
    """Grade the order a proposition "A before B in F" states (``holds``: as written, else reversed).

    Codes: ``order`` (full), ``reversed`` and ``no_call`` (none; exhaustive over F's body),
    ``nested`` (partial: a call sits in a def or lambda nested in F, which runs when it is called),
    ``same_line`` / ``outside_citation`` (partial). None when the proposition is not of that form.
    """
    m = _ORDER_PROP.match(proposition or "")
    if not m:
        return None
    a, b, where = m.group(1), m.group(2), m.group(3)
    first, second = (a, b) if holds else (b, a)
    _shown, s, e, full = _evidence_text(Path(repo) if repo else None, ev)
    if full is None or not s:
        return Grade("none", "cited file cannot be read", "unreadable")
    info = call_order(full, ev["path"], where, first, second, near=(int(s), int(e or s)))
    if info is None:
        return Grade("none", f"{ev['path']} has no Python definition `{where}`", "no_def")
    scope = f"{info['name']} ({ev['path']}:{info['start']}-{info['end']})"
    lines, nested = info["lines"], info.get("nested") or {}
    missing = [x for x in (first, second) if x not in lines and x not in nested]
    if missing:
        return Grade("none", f"no direct call to {', '.join(missing)} in {scope}; calls through other names are "
                             "not followed", "no_call")
    if nested and not (all(x in lines for x in (first, second)) and lines[first] > lines[second]):
        x = next(iter(nested))
        return Grade("partial", f"`{x}` is called inside a function or lambda nested in {scope} (line {nested[x]}); "
                                "it runs when that is called, so the order is not known statically", "nested")
    if nested:  # F's own calls are reversed; a nested call may still run first
        la, lb = lines[first], lines[second]
        return Grade("none", f"in {scope}, `{second}` is first called at line {lb}, before `{first}` at line {la} "
                             "(a nested function or lambda also calls one of them)", "reversed")
    la, lb = lines[first], lines[second]
    if la == lb:
        return Grade("partial", f"`{first}` and `{second}` are first called on the same line {la} of {scope}",
                     "same_line")
    if la > lb:
        return Grade("none", f"in {scope}, `{second}` is first called at line {lb}, before `{first}` at line {la}",
                     "reversed")
    if not (int(s) <= la <= int(e or s) and int(s) <= lb <= int(e or s)):
        return Grade("partial", f"in {scope}, `{first}` (line {la}) comes before `{second}` (line {lb}), but the "
                                "cited lines do not show both calls", "outside_citation")
    return Grade("full", f"in {scope}, `{first}` is first called at line {la} and `{second}` at line {lb} (AST)",
                 "order")


def _behaviour(repo, spec: dict, ev: dict, text: str, subjects: list[str]) -> Grade:
    if evmod.effective_type(ev) in evmod.FILE_TYPES and ev.get("path") and spec.get("proposition") \
            and "holds" in spec:
        g = order_check(repo, ev, str(spec["proposition"]), bool(spec.get("holds")))
        if g is not None:
            return g
    if spec.get("pattern"):
        return _exclusive(repo, spec, ev, text, subjects)
    return _general(repo, ev, text)


def _call_grade(repo, ev: dict, token: str, *, caller: str | None, target_path: str | None,
                target_qual: str | None, relation: str | None) -> Grade:
    shown, a, b, full = _evidence_text(repo, ev)
    if full is None or not a:
        return Grade("none", "cited file cannot be read", "unreadable")
    if not shown.strip():
        return Grade("none", "cited lines are blank", "blank")
    best: Grade | None = None
    for line in range(int(a), int(b or a) + 1):
        if ev["path"].endswith((".py", ".pyi")):
            g = _py_call_grade(full, ev["path"], line, token, caller=caller, target_path=target_path,
                               target_qual=target_qual, relation=relation)
        else:
            g = _other_call_grade(full, ev["path"], line, token, target_path=target_path)
        # the strongest line wins; among equals the first, most specific finding is kept
        if best is None or GRADES.index(g.grade) > GRADES.index(best.grade) or \
                (g.grade == best.grade and best.code == "absent" and g.code != "absent"):
            best = g
        if best.grade == "full":
            break
    return best or Grade("none", f"cited lines do not name `{token}`", "absent")


def _relation(repo, spec: dict, ev: dict, text: str, subjects: list[str]) -> Grade:
    typ = evmod.effective_type(ev)
    b_path, b_sym = _subject_parts(subjects[1] if len(subjects) > 1 else None)
    label = spec.get("target_label") or b_sym or ""
    token = _token(label)
    if not token:
        return _general(repo, ev, text)
    meta = ev.get("meta") or {}
    if typ == "graph_edge":
        if spec.get("source") and spec.get("target") and \
                (meta.get("source"), meta.get("target")) == (spec.get("source"), spec.get("target")):
            return Grade("partial", "the extractor's edge between the claimed endpoints")
        loc = ev.get("locator") or ""
        if spec.get("source") and spec.get("target") and loc.startswith(f"{spec['source']} -[") and \
                loc.endswith(f"]-> {spec['target']}"):
            return Grade("partial", "the extractor's edge between the claimed endpoints")
        return Grade("none", "graph edge between other endpoints")
    if typ == "static_resolution":
        kind = meta.get("kind")
        tgt_ok = (not b_path or meta.get("target_path") in (None, b_path)) and \
            (not meta.get("target_qual") or _token(meta.get("target_qual")) == token)
        if kind == "definitive" and tgt_ok:
            return Grade("full", f"{meta.get('tool', 'resolver')}: unique definition is the target")
        if kind in ("dynamic", "ambiguous") or (kind == "definitive" and not tgt_ok):
            return Grade("partial", f"{meta.get('tool', 'resolver')}: {kind}"
                         + ("" if tgt_ok else " (resolves elsewhere)"))
        return Grade("none", f"{meta.get('tool', 'resolver')}: {kind or 'no answer'}")
    if typ in ("experiment", "test_result") and meta.get("kind") == "call_trace":
        callee_ok = (not b_path or meta.get("callee_path") in (None, b_path)) and \
            _token(meta.get("callee_qual") or meta.get("callee") or token) == token
        site_ok = not spec.get("at") or not meta.get("caller_at") or meta.get("caller_at") == spec.get("at")
        doubles = (meta.get("flags") or {}).get("test_double")
        if meta.get("outcome") == "pass" and callee_ok and site_ok and not doubles:
            return Grade("full", "call observed at runtime")
        if meta.get("outcome") == "pass" and callee_ok:
            return Grade("partial", "call observed" + (" only through a test double" if doubles else " elsewhere"))
        return Grade("none", "runtime trace does not show this call")
    if typ in evmod.FILE_TYPES and ev.get("path"):
        # the caller the claim states (a module caller has no enclosing def to check); a dotted target
        # label ("repo.save", "self._check") names the receiver as the claim writes it
        g = _call_grade(repo, ev, token, caller=claimed_caller(spec, subjects, text), target_path=b_path,
                        target_qual=b_sym or (label if "." in label.strip(".") else None),
                        relation=spec.get("relation"))
        return _python_only(g, ev["path"], "which definition the call binds to and that it sits in the caller")
    return _general(repo, ev, text)


def _python_only(g: Grade, path: str, what: str) -> Grade:
    """A ``full`` grade of source lines outside Python is ``partial``: the binding checks (import scopes, the
    enclosing caller, the name an environment read is assigned to) exist for Python only, so a relation or
    config claim there is ``strong_inference`` at most (a static resolver's definitive answer still
    verifies)."""
    if g.grade != "full" or str(path).endswith((".py", ".pyi")):
        return g
    suffix = PurePosixPath(str(path)).suffix
    return Grade("partial", f"{g.reason}; {what} is checked for Python only, not for "
                            f"{suffix + ' files' if suffix else 'this file'} (strong_inference at most)",
                 g.code or "not_python")


def _location(repo, spec: dict, ev: dict, text: str, subjects: list[str]) -> Grade:
    meta = ev.get("meta") or {}
    name = spec.get("symbol") or meta.get("symbol")
    if not name:
        bt = _BACKTICK.findall(text or "")
        name = bt[0] if bt else None
    if not name:
        _, sym = _subject_parts(subjects[0] if subjects else None)
        if sym and "defined at" in (text or ""):
            name = sym
    if not name:
        return _general(repo, ev, text)
    shown, a, b, full = _evidence_text(repo, ev)
    if full is None or not a:
        return Grade("none", "cited file cannot be read")
    facts = _facts(ev["path"], full)
    if not anchors.usable(facts):
        return Grade("partial", "no syntax facts for this file; the span cannot be checked")
    clean = name.strip().strip("`").strip()
    if "::" in clean:  # `path::symbol` names the file too
        want, _, clean = clean.rpartition("::")
        want = want.replace("\\", "/").strip("/")
        if want and not (ev.get("path") == want or str(ev.get("path") or "").endswith("/" + want)):
            return Grade("none", f"the claim's symbol is in {want}, the evidence cites {ev['path']}")
    if facts.get("lang") == "markdown":
        secs = [(k, s) for k, s in facts.get("sections", {}).items()
                if s["start"] == a and norm_title(s["title"]) == norm_title(clean)]
        if secs:
            s = secs[0][1]
            if s["end"] == b:
                return Grade("full", f"section `{s['title']}` spans {a}-{b}")
            return Grade("partial", f"section `{s['title']}` spans {s['start']}-{s['end']}, not {a}-{b}")
        return Grade("none", f"no heading `{clean}` at line {a}")
    cands = anchors.symbols_named(facts, clean)
    if not cands and facts.get("lang") == "python":
        # a module or class constant (`DISCOUNT_THRESHOLD = ...`) is defined by its assignment
        tree = _py_tree(full)
        spans = assignment_spans(tree, clean) if tree is not None else []
        hit = next((sp for sp in spans if sp[0] == a), None)
        if hit is not None:
            if hit[1] == (b or a):
                return Grade("full", f"`{clean}` is assigned at {a}-{b} (AST)")
            return Grade("partial", f"`{clean}` is assigned at {a}-{hit[1]}, not {a}-{b}")
        if spans:
            return Grade("partial", f"`{clean}` is assigned at line {spans[0][0]}, not at line {a}")
    at = [s for _, s in cands if a in (s["start"], s["def"])]
    if not at:
        return Grade("none" if not cands else "partial",
                     f"no definition of `{clean}` starts at line {a}"
                     + (f" (found at {', '.join(str(s['def']) for _, s in cands[:3])})" if cands else ""))
    s = at[0]
    if s["end"] == b:
        return Grade("full", f"`{clean}` is defined at {a}-{b} (AST)")
    return Grade("partial", f"`{clean}` starts at {a} but ends at {s['end']}, not {b}")


def norm_title(t: str) -> str:
    return " ".join(t.replace("`", "").split()).lower()


def _config(repo, spec: dict, ev: dict, text: str, subjects: list[str]) -> Grade:
    var = spec.get("env") or (ev.get("meta") or {}).get("env")
    if not var:
        return _general(repo, ev, text)
    shown, a, b, full = _evidence_text(repo, ev)
    if full is None or not a:
        return Grade("none", "cited file cannot be read")
    if ev["path"].endswith(".py"):
        tree = _py_tree(full)
        if tree is not None:
            for node in ast.walk(tree):
                lo = getattr(node, "lineno", None)
                if lo is None or not (a <= lo <= (b or a)):
                    continue
                if _is_env_read(node, var):
                    return Grade("full", f"environment read of {var} at line {lo} (AST)")
    else:
        from verinoda.architecture_map import ENV_PATTERNS

        for ln in shown.splitlines():
            for rx, _lang in ENV_PATTERNS:
                if any(m.group(1) == var for m in rx.finditer(ln)):
                    return _python_only(Grade("full", f"environment read of {var} ({_lang} pattern)"), ev["path"],
                                        "the name the read is assigned to")
    if re.search(rf"\b{re.escape(var)}\b", shown):
        return Grade("partial", f"{var} appears, but not as an environment read")
    return Grade("none", f"cited lines do not mention {var}")


def _is_env_read(node: ast.AST, var: str) -> bool:
    return _env_var_of(node) == var


def _env_var_of(node: ast.AST) -> str | None:
    """The variable name an environment read reads (a string literal), else None."""
    def is_environ(e: ast.AST) -> bool:
        return (isinstance(e, ast.Attribute) and e.attr == "environ") or (isinstance(e, ast.Name) and e.id == "environ")

    def lit(e: ast.AST | None) -> str | None:
        return e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None

    if isinstance(node, ast.Call):
        f = node.func
        first = node.args[0] if node.args else None
        if isinstance(f, ast.Attribute) and f.attr in ("get", "setdefault", "pop") and is_environ(f.value):
            return lit(first)
        if (isinstance(f, ast.Attribute) and f.attr in ("getenv", "getenvb")) or \
                (isinstance(f, ast.Name) and f.id in ("getenv", "getenvb")):
            return lit(first)
    if isinstance(node, ast.Subscript) and is_environ(node.value):
        sl = node.slice
        return lit(sl.value if isinstance(sl, ast.Index) else sl)  # type: ignore[attr-defined]
    if isinstance(node, ast.Compare) and any(is_environ(c) for c in node.comparators):
        return lit(node.left)
    return None


def env_bindings(tree: ast.AST, var: str | None = None) -> list[dict]:
    """Environment reads (of ``var``, or of every literal name) and the names each is bound to.

    ``{"var", "line", "names"}``: the assignment targets (``X = int(os.environ.get("V"))``,
    ``self.x = ...``), dict keys and keyword names around the read, then the definitions that
    enclose it, innermost first.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for ch in ast.iter_child_nodes(node):
            parents[ch] = node
    out = []
    for node in ast.walk(tree):
        v = _env_var_of(node)
        if v is None or (var is not None and v != var):
            continue
        names: list[str] = []
        cur = node
        while cur in parents:
            par = parents[cur]
            if isinstance(par, ast.keyword) and par.arg:
                names.append(par.arg)
            elif isinstance(par, ast.Dict):
                names += [k.value for k, val in zip(par.keys, par.values)
                          if val is cur and isinstance(k, ast.Constant) and isinstance(k.value, str)]
            elif isinstance(par, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (par.targets if isinstance(par, ast.Assign) else [par.target]):
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name) and n.id not in ("self", "cls"):
                            names.append(n.id)
                        elif isinstance(n, ast.Attribute):
                            names.append(n.attr)
                        elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                                and isinstance(n.slice.value, str):
                            names.append(n.slice.value)
            elif isinstance(par, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(par.name)
            cur = par
        out.append({"var": v, "line": node.lineno, "names": list(dict.fromkeys(names))})
    return sorted(out, key=lambda b: b["line"])


def config_binding(repo, ev: dict, var: str, text: str) -> dict | None:
    """Is the subject of a config claim's text the name the cited read of ``var`` is bound to?

    ``{"ok", "subject", "reads", "alt", "why"}``, or None when there is nothing to check (not Python, no
    read of ``var`` at the cited lines, or the text names no subject besides the variable).
    ``alt`` is another read in the same file whose binding spells the whole subject: the text is
    about that setting, which reads another variable.
    """
    if not str(ev.get("path") or "").endswith(".py"):
        return None
    _shown, a, b, full = _evidence_text(Path(repo) if repo else None, ev)
    if full is None or not a:
        return None
    return binding_check(ev["path"], full, var, text, (int(a), int(b or a)))


def binding_check(rel: str, full: str, var: str, text: str, lines: tuple[int, int] | None = None) -> dict | None:
    """:func:`config_binding` on a file's text (the reads of ``var`` within ``lines``, or anywhere)."""
    subject = config_subject(text, var)
    tree = _py_tree(full) if subject else None
    if tree is None:
        return None
    every = env_bindings(tree)
    reads = [r for r in every if r["var"] == var and (lines is None or lines[0] <= r["line"] <= lines[1])]
    if not reads:
        return None
    hit = max(reads, key=lambda r: len(binds(r["names"], subject)))
    got = binds(hit["names"], subject)
    loc = f"{rel}:{hit['line']}"
    if 2 * len(got) >= len(subject):
        return {"ok": True, "subject": subject, "reads": reads, "alt": None,
                "why": f"{loc} binds {var} to {', '.join(hit['names'][:2])}"}
    alt = None
    if not got:  # another read whose binding spells the whole subject: the text is about that setting
        alts = [r for r in every if r["var"] != var and r["names"] and len(binds(r["names"], subject)) == len(subject)]
        alt = alts[0] if alts else None
    why = f"{loc} binds {var} to {hit['names'][0] if hit['names'] else 'no name'}"
    if alt is not None:
        why += f"; {alt['names'][0]} reads {alt['var']} at {rel}:{alt['line']} (scope: environment reads in {rel})"
    else:
        why += f", which does not name '{' '.join(subject)}'"
    return {"ok": False, "subject": subject, "reads": reads, "alt": alt, "why": why}


def _flow(repo, spec: dict, ev: dict, text: str, subjects: list[str]) -> Grade:
    meta = ev.get("meta") or {}
    hops = spec.get("hops") or []
    if meta.get("hop"):
        frm, _, to = str(meta["hop"]).partition("->")
        hop = next((h for h in hops if h.get("from") == frm and h.get("to") == to), None)
        relation = (hop or {}).get("relation") or "calls"
        if relation == "method":
            shown, a, b, full = _evidence_text(repo, ev)
            facts = _facts(ev["path"], full) if full is not None else None
            if a and any(a in (s["start"], s["def"]) for _, s in anchors.symbols_named(facts, _token(to))):
                return Grade("full", f"`{_token(to)}` is defined at line {a}")
            return Grade("partial", f"method hop {frm}->{to}: no definition at the cited line")
        # a call hop is a relation claim with the same binding checks: outside Python strong_inference at most
        g = _call_grade(repo, ev, _token(to), caller=_token(frm) or None, target_path=None, target_qual=None,
                        relation=relation)
        return _python_only(g, ev["path"], "which definition the hop's call binds to and that it sits in the caller")
    if meta.get("sink"):
        from verinoda.architecture_map import SINK_PATTERNS

        shown, *_ = _evidence_text(repo, ev)
        kinds = set(spec.get("sink_kinds") or [])
        for rx, kind in SINK_PATTERNS:
            if (not kinds or kind in kinds) and rx.search(shown):
                return Grade("full", f"sink line matches the {kind} pattern")
        return Grade("none", "sink line shows none of the claimed sink operations")
    return _general(repo, ev, text)


def _exclusive(repo, spec: dict, ev: dict, text: str, subjects: list[str]) -> Grade:
    pat = spec.get("pattern")
    if not pat:
        return _general(repo, ev, text)
    shown, *_ = _evidence_text(repo, ev)
    try:
        rx = re.compile(pat)
    except re.error:
        return Grade("none", "the claim's pattern is not a valid regex")
    if any(rx.search(ln) for ln in shown.splitlines()):
        return Grade("full", f"cited line matches /{pat}/")
    return Grade("none", f"cited lines do not match /{pat}/")


def _tests(repo, spec: dict, ev: dict, text: str, subjects: list[str]) -> Grade:
    test = (ev.get("meta") or {}).get("test")
    shown, *_ = _evidence_text(repo, ev)
    if test and re.search(rf"\bdef\s+{re.escape(_token(test))}\b|\b{re.escape(_token(test))}\b", shown):
        return Grade("partial", f"cited line defines test `{_token(test)}` (static reachability is inference)")
    return cap_grade(_general(repo, ev, text), "partial")


def _run_targets(ev: dict) -> list[str]:
    loc = ev.get("locator") or ""
    cmd = loc.split(": ", 1)[1] if ": " in loc else loc
    return [t for t in cmd.split() if ".py" in t or t.startswith("tests")]


def _import_closure(repo: Path | None, files: list[str], depth: int = 4) -> set[str]:
    """In-repo Python files reachable from ``files`` through imports (a static over-approximation)."""
    if repo is None:
        return set(files)
    seen = set(files)
    frontier = list(files)
    for _ in range(depth):
        nxt = []
        for f in frontier:
            try:
                tree = anchors.parse_python((Path(repo) / f).read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError, ValueError):
                continue
            mods = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods += [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        pkg = list(PurePosixPath(f).parent.parts)
                        pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                        base = ".".join([*pkg, *([base] if base else [])])
                    mods.append(base)
                    mods += [f"{base}.{a.name}" for a in node.names]
            for m in mods:
                rel = m.replace(".", "/")
                for cand in (f"{rel}.py", f"{rel}/__init__.py", f"src/{rel}.py", f"src/{rel}/__init__.py"):
                    if cand not in seen and (Path(repo) / cand).is_file():
                        seen.add(cand)
                        nxt.append(cand)
        frontier = nxt
    return seen


def _test_run(repo, spec: dict, ev: dict, text: str, subjects: list[str]) -> Grade:
    typ = evmod.effective_type(ev)
    if typ not in ("experiment", "test_result"):
        return _general(repo, ev, text)
    meta = ev.get("meta") or {}
    loc = ev.get("locator") or ""
    ids = [str(x) for x in (spec.get("command") or spec.get("tests") or [])]
    names_run = bool(spec.get("experiment")) and spec.get("experiment") == meta.get("experiment_id")
    if not names_run and ids:
        names_run = all(i in loc for i in ids)
    targets = _run_targets(ev)
    test_files = sorted({t.split("::")[0] for t in targets if t.split("::")[0].endswith(".py")})
    closure = _import_closure(Path(repo) if repo else None, test_files)
    missing = []
    for s in subjects or []:
        path, sym = _subject_parts(s)
        if not path:
            continue
        if path in test_files or any(t.startswith(path) for t in targets):
            continue
        if path in closure:
            continue
        if sym and repo is not None and any(
                re.search(rf"\b{re.escape(_token(sym))}\b", _safe_read(Path(repo) / f)) for f in test_files):
            continue
        missing.append(s)
    if names_run and not missing:
        return Grade("full", "the claim names this run and its subjects are exercised by the run's tests")
    if names_run:
        return Grade("partial", "the run is the claim's, but it does not exercise: " + ", ".join(missing[:4]))
    return Grade("none", "the claim's spec does not name this run")


def _safe_read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _impact(repo, spec: dict, ev: dict, text: str, subjects: list[str]) -> Grade:
    if evmod.effective_type(ev) == "graph_edge":
        p = ev.get("path") or ""
        if p and (p in (text or "") or p in (subjects or [])):
            return Grade("partial", f"dependency edge from {p} (static impact is inference)")
        return Grade("none", "edge does not involve the claimed files")
    return cap_grade(_general(repo, ev, text), "partial")


_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n\s*\n")


def _negated(sentence: str) -> bool:
    words = {fold_tr(w.strip("'\u2019")) for w in re.findall(r"[\w'\u2019]+", sentence)}
    return bool(words & NEGATIONS) or "n't" in sentence.lower()


def _polarity_ok(claim: str, shown: str) -> bool:
    """Every negated sentence of the claim has a negated evidence sentence that states its terms."""
    ev_neg = [s for s in _SENTENCE.split(shown or "") if _negated(s)]
    for sent in _SENTENCE.split(claim or ""):
        if not _negated(sent):
            continue
        t = claim_terms(sent)
        if not any(not coverage(t, s)["missing"] for s in ev_neg):
            return False
    return True


def _documentary(kind: str, repo, spec: dict, ev: dict, text: str) -> Grade:
    typ = evmod.effective_type(ev)
    terms = claim_terms(text)
    if typ == "git_history":
        sha = (ev.get("commit_sha") or "").lower()
        named = [h for h in _HEX.findall((text or "").lower()) if sha.startswith(h)]
        if sha and named:
            return Grade("partial", f"the claim names commit {named[0]} (a record's content is not entailed "
                                    "mechanically)")
        return Grade("none", "the claim does not name this commit")
    shown, a, b, full = _evidence_text(repo, ev)
    paths_ok = not terms.paths or not ev.get("path") or any(ev["path"].endswith(p) or p.endswith(ev["path"])
                                                            for p in terms.paths)
    if not paths_ok:
        return Grade("none", "the claim names another document")
    if terms.negated and not _polarity_ok(text, shown):
        return Grade("none", "the claim negates something the record does not negate")
    cov = coverage(terms, shown + "\n" + _context_text(repo, ev, a, b, full))
    if not (terms.keys or terms.words):
        return Grade("none", "the claim has no checkable terms")
    if not cov["missing"]:
        return Grade("partial", "the record states every term the claim attributes to it "
                                "(a rationale is not entailed mechanically)")
    return Grade("none", "the record does not state: " + ", ".join(cov["missing"][:6]))


def _general(repo, ev: dict, text: str) -> Grade:
    shown, a, b, full = _evidence_text(repo, ev)
    if evmod.effective_type(ev) in evmod.FILE_TYPES and ev.get("path") and not (shown or "").strip():
        return Grade("none", "cited lines are blank")
    terms = claim_terms(text)
    locator_ok = True
    if terms.locators and ev.get("path"):
        locator_ok = any((ev["path"].endswith(p) or p.endswith(ev["path"])) and
                         (a is None or (s <= int(b or a) and int(a) <= e)) for p, s, e in terms.locators)
    elif terms.locators and not ev.get("path"):
        locator_ok = False
    quote = re.search(r"\bcontains:\s*(.+)$", text or "", re.S)
    if quote and locator_ok and ev.get("path"):
        q = " ".join(quote.group(1).split()).rstrip(".")
        cited = " ".join((shown or "").split())
        # a short quote only as the whole cited text ("config.toml:24 contains: [debug]")
        if (len(q) >= 8 and q in cited) or (re.search(r"\w", q) and q == cited.rstrip(".")):
            return Grade("full", "the cited lines contain the quoted text verbatim", "quote")
    ctx = _context_text(repo, ev, a, b, full)
    return _coverage_grade(terms, f"{shown}\n{ctx}", locator_ok=locator_ok)


def text_conflicts(repo, text: str, ev: dict) -> list[str]:
    """Heuristic contradictions between a plain-text claim and the lines it cites.

    A number the claim states is absent while the lines carry other numbers,
    or the claim negates what the lines state without a negation (all other
    terms covered in both cases). Used by critique as a heuristic warning,
    never as a refutation.
    """
    shown, a, b, full = _evidence_text(repo, ev)
    if not (shown or "").strip():
        return []
    terms = claim_terms(text)
    cov = coverage(terms, f"{shown}\n{_context_text(repo, ev, a, b, full)}")
    out = []
    nums = [k for k in terms.keys if _num(k) is not None]
    missing_nums = [k for k in nums if k in cov["missing"]]
    if missing_nums and (cov["keys_hit"] or cov["words_hit"]):
        ev_nums = sorted({t for t in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", shown)} - set(nums))
        if ev_nums:
            out.append(f"the cited lines state {', '.join(ev_nums[:4])}, not {', '.join(missing_nums)}")
    if terms.negated and not cov["missing"] and not _negated(shown):
        out.append("the cited lines state it without the negation")
    return out


def cap_grade(g: Grade, ceiling: str) -> Grade:
    return g if GRADES.index(g.grade) <= GRADES.index(ceiling) else Grade(ceiling, g.reason)


# What written text may state beyond a typed check: an order, a condition, a count.
_ORDER_WORDS = re.compile(r"\b(?:before|after|afterwards|then|first|firstly|last|lastly|finally|earlier|later|önce|"
                          r"sonra|ardından|ilk)\b", re.I)
_COND_WORDS = re.compile(r"\b(?:if|unless|when|whenever|once|until|provided|providing|assuming|given\s+that|"
                         r"as\s+long\s+as|in\s+case|otherwise|except|eğer|halinde|durumunda|sürece|iken)\b", re.I)
# Turkish conditional verb and copula forms (gelirse, olmazsa, altındaysa, varsa, yoksa, açıksa), read
# only in Turkish text: English words end the same way (parse, reverse, analyse)
_TR_COND = re.compile(r"\b(?!(?:false|else|pulse|impulse|response|license|licence|sense|dense|tense|intense|immense|"
                      r"expense|defense|defence|offense|rinse|parse|sparse|reverse|traverse|course|horse|worse|purse|"
                      r"nurse|verse|universe|diverse|inverse|converse|adverse|disperse|immerse|terse|endorse|coarse|"
                      r"rehearse|lapse|collapse|eclipse|ellipse|glimpse|analyse|dispense|suspense|condense|nonsense|"
                      r"browse)\b)[\wçğıöşü]*(?:[bcçdfgğhjklmnprştvyz]s[ae]|ys[ae])\b", re.I)
_TR_TEXT = re.compile(r"[çğıöşüÇĞİÖŞÜ]|\b(?:ve|bir|bu|ile|için|olarak|tarafından)\b", re.I)
# a bound on a value ("for subtotals below the threshold"): a condition the kinds do not check
_BOUND_WORDS = re.compile(r"\b(?:below|above|greater\s+than|less\s+than|more\s+than|fewer\s+than|exceeds?|exceeding|"
                          r"at\s+least|at\s+most)\b", re.I)
_COUNT_WORDS = re.compile(r"\b(?:twice|thrice|times|kez|kere|defa)\b", re.I)
_LINE_NUMBER = re.compile(r"\b(?:lines?|satır\w*)\s*\d+(?:\s*[-–]\s*\d+)?", re.I)
# numbers written as words ("ten seconds", "two arguments"; not "one", TR "bir"/"on"/"altı"/"yüz"/"bin",
# which are also other words)
_NUMBER_WORDS = re.compile(r"\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
                           r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
                           r"eighty|ninety|hundred|thousand|million|billion|dozen|sıfır|iki|üç|dört|beş|yedi|sekiz|"
                           r"dokuz|yirmi|otuz|kırk|elli|altmış|yetmiş|seksen|doksan|milyon)\b", re.I)
# an order claim that says the two calls are adjacent: the order check proves only which comes first
_ADJACENT = re.compile(r"\b(?:immediately|right\s+(?:before|after)|just\s+(?:before|after)|directly\s+(?:before|after)|"
                       r"straight\s+after|hemen|doğrudan\s+(?:önce|sonra))\b", re.I)
# the arguments or the manner of a call ("with two arguments", "passing `items`"; TR "müşteri adıyla")
_CALL_MANNER = re.compile(r"\b(?:with|passing|using)\s+`?(?!(?:the\s+)?(?:same|its|no)\b)\w+", re.I)
_TR_WITH = re.compile(r"\b(?!(?:böyle|şöyle|öyle)\b)[\wçğıöşü]+(?:yla|yle)\b", re.I)


def unchecked_statements(kind: str, text: str) -> tuple[list[str], list[str]]:
    """``(problems, numbers)``: what written text states that the typed check of ``kind`` does not
    establish - a negation (the check proves the positive statement), a quantifier ("only", "all",
    "always"), an order (except for the order kind itself; there: that the calls are adjacent,
    "immediately before"), a condition or a bound ("provided that", "below the threshold", TR
    "altındaysa"), a count, the arguments of a call ("with two arguments", relation claims) - and the
    numbers it states, digits or words (besides line numbers), which the caller compares where the
    kind can (a config default)."""
    prose = _LINE_NUMBER.sub(" ", _prose(text))
    out = []
    neg = negated_text(text)
    if neg:
        out.append(f"the text is negated ('{neg}'); the {kind} check establishes only the positive statement")
    words = {fold_tr(w) for w in re.findall(r"\w+", prose)}
    quant = sorted(words & QUANTIFIERS - ({"only"} if kind == "behaviour" else set()))
    if quant:
        out.append(f"the text states '{quant[0]}', which the {kind} check does not establish")
    if kind != "behaviour" and (m := _ORDER_WORDS.search(prose)):
        out.append(f"the text states an order ('{m.group(0)}'), which the {kind} check does not verify "
                   "(an order claim is `--kind order`)")
    if kind == "behaviour" and (m := _ADJACENT.search(prose)):
        out.append(f"the text states that the calls are adjacent ('{m.group(0)}'); the order check establishes "
                   "only which comes first")
    turkish = bool(_TR_TEXT.search(prose))
    bare = _LOCATOR.sub(" ", _PATHLIKE.sub(" ", text or ""))  # code names kept: "with `items`" is an argument
    checks = [(_COND_WORDS, prose, "a condition"), (_BOUND_WORDS, prose, "a bound"), (_COUNT_WORDS, prose, "a count")]
    if turkish:
        checks.append((_TR_COND, prose, "a condition"))
    if kind == "relation":
        checks.append((_CALL_MANNER, bare, "how the call is made"))
        if turkish:
            checks.append((_TR_WITH, prose, "how the call is made"))
    said = set()
    for rx, where, what in checks:
        m = rx.search(where)
        if m and what not in said:
            said.add(what)
            out.append(f"the text states {what} ('{m.group(0)}'), which the {kind} check does not verify")
    nums = re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", prose) + _NUMBER_WORDS.findall(prose)
    return out, nums


def _env_read_text(full: str, var: str, a: int, b: int) -> str | None:
    """The source of the environment reads of ``var`` within lines a-b (their defaults included), or None."""
    tree = _py_tree(full)
    if tree is None:
        return None
    segs = [ast.get_source_segment(full, n) or "" for n in ast.walk(tree)
            if a <= getattr(n, "lineno", 0) <= b and _env_var_of(n) == var]
    return "\n".join(segs) if segs else None


def _enclosing_names(full: str, rel: str, line: int) -> set[str] | None:
    """Names of the definitions around ``line`` (folded), or None when the language has no facts."""
    if rel.endswith((".py", ".pyi")):
        tree = _py_tree(full)
        return {fold_tr(d.name) for d in _enclosing_def(tree, line)} if tree is not None else None
    facts = _facts(rel, full)
    if not anchors.usable(facts):
        return None
    return {fold_tr(p) for q, s in facts.get("symbols", {}).items() if s["start"] <= line <= s["end"]
            for p in re.split(r"[.#]", q) if p}


def _plain_caller_problems(repo, ev: dict, parsed: dict) -> list[str]:
    """A caller the text names by a plain word (``checkout``) or a file must be where the cited lines are:
    a definition around them, or their file."""
    path = str(ev.get("path") or "")
    if parsed["caller_file"]:
        cf = parsed["caller_file"]
        return [] if path == cf or path.endswith("/" + cf) else [f"the text's caller is {cf}, the evidence is in "
                                                                 f"{path}"]
    word = parsed["caller_word"]
    try:
        _t, a, _b, full = _evidence_text(Path(repo) if repo else None, ev)
        names = _enclosing_names(full, path, int(a)) if full and a else None
    except (OSError, ValueError, TypeError):
        names = None
    if names is None:
        return [f"the text's caller `{word}` is a plain word, not checked against the cited lines"]
    if fold_tr(word) in names or fold_tr(word) == fold_tr(PurePosixPath(path).stem):
        return []
    return [f"the text's caller `{word}` is not a definition around the cited lines"]


def plain_caller_encloses(repo, path: str, line: int, word: str) -> bool:
    """Is ``word``, a caller written text names as a plain word ("checkout calls submit"), the name of
    a definition around ``line`` of ``path``? Then it is that definition, as if written as code."""
    if repo is None or not word:
        return False
    try:
        full = (Path(repo) / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    names = _enclosing_names(full, path, int(line))
    return bool(names) and fold_tr(word) in names


def _direct_call(repo, path: str, caller: str | None, callee: str | None) -> dict | None:
    """:func:`caller_scope` of ``caller`` for ``callee`` in ``path``, when its whole body calls it directly."""
    if not (caller and callee and path):
        return None
    try:
        sc = caller_scope(repo, caller, _token(callee), path=path)
    except (OSError, ValueError, RecursionError):
        return None
    return sc if sc and sc["calls"] else None


_OWNER_WORD = re.compile(r"\b([A-Z][\w]*)`?\s+(?:class|sınıf\w*)\b|\bclass\s+`?([A-Z][\w]*)")


def text_paths(text: str) -> list[str]:
    """The file paths written text names (``orders/service.py``, ``service.py``, ``pricing.py:14``)."""
    out = [m.group(1) for m in _LOCATOR.finditer(text or "")]
    out += _PATHLIKE.findall(_LOCATOR.sub(" ", text or ""))
    return list(dict.fromkeys(p.replace("\\", "/").lstrip("./") for p in out))


def other_file_problems(kind: str, spec: dict, ev: dict, text: str, subjects: list[str]) -> list[str]:
    """A file the text names is a role: when it is not the evidence file (nor another cited file, nor
    the file of ``--symbol path::name``), the check did not establish what the text says about it
    ("`save` is defined in orders/service.py" with the evidence in orders/repository.py)."""
    path = str(ev.get("path") or "")
    known = {path, *(str(s).partition("::")[0] for s in subjects or [] if s)}
    sym = str(spec.get("symbol") or "")
    if "::" in sym:
        known.add(sym.rpartition("::")[0].replace("\\", "/").strip("/"))
    if kind == "relation":  # a caller written as a file is checked by the relation rules
        known.add(str((relation_parse(text, spec.get("target_label")) or {}).get("caller_file") or ""))
    known = {k for k in known if k}
    out = []
    for p in text_paths(text):
        if not any(k == p or k.endswith("/" + p) or p.endswith("/" + k) for k in known):
            out.append(f"the text names {p}, the evidence is in {path}")
    return out


# The kind of a definition as written text states it. Two-word forms first ("module constant",
# "class attribute"); the words for the owner ("the `Settings` class", "class Cart") are not a kind.
_KIND_PHRASES = (
    ("module_level", r"module[\s-]+(?:level|constant|variable)s?|top[\s-]+level|global\s+(?:constant|variable)s?|"
                     r"modül\s+(?:düzeyinde|seviyesinde|sabiti|değişkeni)\w*"),
    ("class_level", r"class[\s-]+(?:level|attribute|constant|variable|field|member|property)s?|"
                    r"sınıf\s+(?:düzeyinde|değişkeni|sabiti|özelliği|alanı)\w*"),
    ("instance", r"instance\s+(?:attribute|variable|field)s?"),
    ("class", r"class(?:es)?|sınıf\w*|sinif\w*"),
    ("function", r"functions?|fonksiyon\w*"),
    ("method", r"methods?|metot\w*|metod\w*|yöntem\w*"),
    ("constant", r"constants?|sabit\w*"),
    ("variable", r"variables?|değişken\w*"),
    ("attribute", r"attributes?|fields?|özellik\w*"),
    ("property", r"propert(?:y|ies)"),
    ("async", r"async|asynchronous|coroutines?|asenkron\w*"),
    ("generator", r"generators?"),
    ("static", r"static(?:method)?"),
    ("classmethod", r"classmethods?"),
    ("abstract", r"abstract"),
    ("decorator", r"decorators?"),
    ("lambda", r"lambdas?"),
    ("enum", r"enums?"),
    ("interface", r"interfaces?"),
    ("struct", r"structs?"),
)
_KIND_RX = re.compile("|".join(f"(?P<{k}>\\b(?:{rx})\\b)" for k, rx in _KIND_PHRASES), re.I)
_OWNER_PHRASE = re.compile(r"(?:\b[A-Z][\w]*|`[^`]+`)\s+(?:class|sınıf\w*|module|modül\w*)\b|"
                           r"\b(?:class|module)\s+(?:\b[A-Z][\w]*|`[^`]+`)")


def stated_kinds(text: str) -> list[str]:
    """The kinds of definition written text states ("a function", "an async method", "a module
    constant"), without the words that name an owner ("in the `OrderRepository` class")."""
    t = _BACKTICK.sub(" ", _OWNER_PHRASE.sub(" ", text or ""))
    return list(dict.fromkeys(m.lastgroup for m in _KIND_RX.finditer(t)))


def _py_def_kinds(full: str, name: str, line: int) -> set[str] | None:
    """The kind words a Python definition of ``name`` starting at ``line`` allows, or None when there
    is none there (or the file does not parse)."""
    tree = _py_tree(full)
    if tree is None:
        return None

    def deco_names(d: ast.AST) -> set[str]:
        return {getattr(x, "attr", None) or getattr(x, "id", None) or "" for x in
                (getattr(e, "func", e) for e in d.decorator_list)}

    def visit(node: ast.AST, in_class: bool) -> set[str] | None:
        for ch in ast.iter_child_nodes(node):
            starts = {getattr(ch, "lineno", 0), *(e.lineno for e in getattr(ch, "decorator_list", []))}
            if isinstance(ch, ast.ClassDef) and ch.name == name and line in starts:
                return {"class", "attribute"} if in_class else {"class"}
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) and ch.name == name and line in starts:
                out = {"function"} | ({"method"} if in_class else set())
                if isinstance(ch, ast.AsyncFunctionDef):
                    out.add("async")
                if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(ch)):
                    out.add("generator")
                decos = deco_names(ch)
                out |= {k for k, d in (("static", "staticmethod"), ("classmethod", "classmethod"),
                                       ("property", "property"), ("abstract", "abstractmethod")) if d in decos}
                return out
            if isinstance(ch, (ast.Assign, ast.AnnAssign)) and ch.lineno == line:
                targets = ch.targets if isinstance(ch, ast.Assign) else [ch.target]
                if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                    return ({"constant", "variable", "attribute", "property", "class_level"} if in_class
                            else {"constant", "variable", "module_level"})
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # a local of a function is no definition of the module or class
            found = visit(ch, in_class or isinstance(ch, ast.ClassDef))
            if found is not None:
                return found
        return None

    return visit(tree, False)


def kind_problems(ev: dict, full: str | None, text: str, name: str, line: int | None) -> list[str]:
    """The kind of definition written text states ("`OrderRepository` is a function", "an async
    function", "a module constant") against the definition at the cited line: Python by its syntax
    tree, other languages by their syntax facts (class or def). A kind that differs, or that cannot be
    read, is something the location check did not establish."""
    said = stated_kinds(text)
    if not said:
        return []
    path = str(ev.get("path") or "")
    bare = name.strip().strip("`").rpartition("::")[2].split("(")[0].rpartition(".")[2]
    allowed: set[str] | None = None
    if full and line and path.endswith((".py", ".pyi")):
        allowed = _py_def_kinds(full, bare, int(line))
    elif full and line:
        facts = _facts(path, full)
        if anchors.usable(facts):
            hit = [(q, s) for q, s in anchors.symbols_named(facts, bare) if int(line) in (s["start"], s["def"])]
            if hit:
                q, s = hit[0]
                allowed = {"class"} if s["kind"] == "class" else ({"function", "method"} if "." in q else {"function"})
    words = {k: k.replace("_", " ") for k in said}
    if allowed is None:
        return [f"the text states it is a {', '.join(words.values())}, which the location check does not read here"]
    off = [words[k] for k in said if k not in allowed]
    if not off:
        return []
    return [f"the text states the kind '{', '.join(off)}', which the definition at {path}:{line} does not have"]


def unchecked_names(repo, kind: str, spec: dict, ev: dict, text: str, subjects: list[str]) -> list[str]:
    """Code names written text states besides the roles the typed check of ``kind`` binds: a relation's
    caller and callee, an order's function and its two calls, a location's symbol, and the definitions
    around the cited lines (a method's class). A relation's other callee ("A calls B and C") or another
    clause ("A calls B, and C calls D") counts when the caller's whole body calls it directly
    (:func:`caller_scope`); any other name is something the check does not establish."""
    if kind not in ("relation", "location", "behaviour"):
        return []
    roles: list[str | None] = [spec.get("symbol"), spec.get("target_label"), spec.get("source_label")]
    roles += [_subject_parts(s)[1] for s in subjects]
    if kind == "behaviour":
        m = _ORDER_PROP.match(str(spec.get("proposition") or ""))
        if not m:
            return []  # a pattern claim: its text is not read for names
        roles += list(m.groups())
    parsed = relation_parse(text, spec.get("target_label")) if kind == "relation" else None
    if parsed:
        roles += [parsed["caller"], parsed["caller_word"], parsed["callee"]]
    bound = {fold_tr(_token(r)) for r in roles if r}
    path = str(ev.get("path") or "")
    try:
        _t, a, _b, full = _evidence_text(Path(repo) if repo else None, ev)
        bound |= (_enclosing_names(full, path, int(a)) if full and a else None) or set()
    except (OSError, ValueError, TypeError):
        pass
    # a plain word that names a class ("a method of the Settings class", "Settings sınıfının") is a name too
    owners = [w for w in (m.group(1) or m.group(2) for m in _OWNER_WORD.finditer(text or ""))
              if fold_tr(w) not in _NOT_A_NAME]
    extra = [n for n in dict.fromkeys([*(n for _, _, n in code_names(text)), *owners])
             if n.rpartition(".")[2].lower() not in FILE_EXTS and fold_tr(_token(n)) not in bound]
    if not extra or kind != "relation" or repo is None:
        return extra
    ok: set[str] = set()
    caller = claimed_caller(spec, subjects, text) or (parsed or {}).get("caller_word")
    for n in extra:  # "A calls B and C": C is another callee of the same caller
        if parsed and n in parsed["callees"] and _direct_call(repo, path, caller, n):
            ok.add(n)
    for ca, cb in _clauses(text):  # "A calls B, and C calls D": the other clause, checked on its own
        p = relation_parse(text[ca:cb])
        if p and p["caller"] and p["callee"] and _direct_call(repo, path, p["caller"], p["callee"]):
            ok |= {n for n in extra if _token(n) in (_token(p["caller"]), _token(p["callee"]))}
    return [n for n in extra if n not in ok]


# Words that frame a quote without adding to it ("the cited source text shows verbatim: ...").
_QUOTE_FRAME = frozenset(_stem(w) for w in "cited shown show shows source text verbatim exactly literally".split())


def _quote_rest_problem(repo, ev: dict, text: str) -> str | None:
    """A verbatim quote verifies only the quoted text: what the claim says besides it and a locator
    (path:line, or the name of the definition around the cited lines) is not verified by it."""
    quote = re.search(r"\bcontains:\s*(.+)$", text or "", re.S)
    if not quote:
        return None
    rest = text[:quote.start()].strip()
    terms = claim_terms(rest)
    names: set[str] = set()
    try:
        _t, a, _b, full = _evidence_text(Path(repo) if repo else None, ev)
        names = (_enclosing_names(full, str(ev.get("path") or ""), int(a)) if full and a else None) or set()
    except (OSError, ValueError, TypeError):
        pass
    extra = [k for k in terms.keys if k.rpartition(".")[2] not in names] + \
        [w for w in terms.words if w not in _QUOTE_FRAME]
    if not extra and not terms.negated and not terms.quantified:
        return None
    said = " ".join(rest.split())
    return (f"the quote verifies only the quoted text, not '{said[:80]}{'...' if len(said) > 80 else ''}' "
            "(term coverage at most)")


_PREDICATES = {
    "relation": _relation, "location": _location, "config": _config, "flow": _flow, "exclusive": _exclusive,
    "tests": _tests, "test_run": _test_run, "impact": _impact, "behaviour": _behaviour,
}


def typed(kind: str, spec: dict | None, text: str | None = None) -> bool:
    """Does a claim of this kind and spec get a typed check (not term coverage)?"""
    spec = spec or {}
    if kind == "relation":
        return bool(_token(spec.get("target_label")))
    if kind == "location":
        return bool(spec.get("symbol") or _BACKTICK.search(text or ""))
    if kind == "config":
        return bool(spec.get("env"))
    if kind == "behaviour":
        return bool(spec.get("pattern") or (spec.get("proposition") and "holds" in spec))
    if kind == "exclusive":
        return bool(spec.get("pattern"))
    return kind in ("flow", "test_run")
_CACHE: dict[str, Grade] = {}


def _cache_key(kind: str, repo, spec: dict, ev: dict, text: str | None, subjects) -> str | None:
    stamp = None
    root = _root(Path(repo) if repo else None, ev)
    if ev.get("path") and root is not None:
        try:
            st = (root / ev["path"]).stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = "missing"
    try:
        return json.dumps([kind, str(repo), spec, ev.get("id"), ev.get("source_type"), ev.get("locator"),
                           ev.get("path"), ev.get("line_start"), ev.get("line_end"), ev.get("content_hash"),
                           ev.get("commit_sha"), ev.get("excerpt"), ev.get("meta") or {}, text,
                           list(subjects or []), stamp], sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


def assess(kind: str, repo: Path | str | None, spec: dict | None, evidence_row: dict, *,
           text: str | None = None, subjects: list[str] | None = None) -> Grade:
    """Grade one evidence row for a claim (see the module docstring), with the reason."""
    spec = {k: v for k, v in (spec or {}).items() if k not in ("assessed", "ceiling", "penalty")}
    subjects = list(subjects or [])
    text = text or ""
    key = _cache_key(kind, repo, spec, evidence_row, text, subjects)
    if key is not None and key in _CACHE:
        return _CACHE[key]
    ev = evidence_row
    typ = evmod.effective_type(ev)
    if typ in evmod.NOT_SUPPORT:
        g = Grade("none", f"{typ} is a pointer to evidence, not evidence")
    elif kind in DOCUMENTARY:
        g = _documentary(kind, repo, spec, ev, text)
    else:
        fn = _PREDICATES.get(kind)
        try:
            g = fn(repo, spec, ev, text, subjects) if fn else _general(repo, ev, text)
        except (OSError, ValueError, RecursionError) as exc:  # never let a grader error verify anything
            g = Grade("none", f"could not grade: {type(exc).__name__}")
    g = cap_grade(g, KIND_MAX.get(kind, "full"))
    if spec.get("free_text") and kind not in DOCUMENTARY and kind != "general" and g.grade != "none":
        # User-written text (`claim add`): the kind predicate proves the code shape for the named
        # symbol, but the text must be about it too - it must name that symbol, and its key terms
        # (identifiers, numbers, product names such as PostgreSQL or AES-256) must occur in the
        # cited lines. Otherwise "Orders are stored in PostgreSQL" could ride on any config line
        # (acceptance audit 2026-09-23). Failing text caps the grade at partial (no verification).
        problems = []
        sym = _token(spec.get("symbol") or spec.get("target_label") or spec.get("env") or "")
        if sym and not re.search(rf"(?<![\w]){re.escape(fold_tr(sym))}(?![\w])", fold_tr(text)):
            problems.append(f"the claim text does not name '{sym}'")
        if kind == "relation" and sym and not (subjects and "::" in subjects[0]) and not spec.get("source_label"):
            # the roles the text states: one caller, and the target as what is called
            parsed = relation_parse(text, spec.get("target_label"))
            if parsed is None:
                problems.append("the text does not state one caller and the callee in a form that is checked "
                                "(\"A calls B\", \"B is called by A\", \"A, B'yi çağırır\", \"B, A tarafından "
                                "çağrılır\")")
            elif parsed["reversed"]:
                problems.append(f"the text makes `{sym}` the caller, not the callee")
            elif parsed["caller_word"] or parsed["caller_file"]:
                problems += _plain_caller_problems(repo, ev, parsed)
        if kind == "config" and spec.get("env") and evmod.effective_type(ev) in evmod.FILE_TYPES:
            try:
                bound = config_binding(repo, ev, spec["env"], text)
            except (OSError, ValueError, RecursionError):
                bound = None
            if bound is not None and not bound["ok"]:
                problems.append(bound["why"])
        try:
            _t, _a, _b, _full = _evidence_text(Path(repo) if repo else None, ev)
            ev_txt = _full or _t or ""  # key terms may sit in the enclosing def (the caller's name)
        except (OSError, ValueError, TypeError):
            _full, _a, _b, ev_txt = None, None, None, ""
        terms = claim_terms(text)
        missing = coverage(Terms(terms.keys, [], [], [], False, False), ev_txt)["missing"]
        if missing:
            problems.append("key terms not in the cited file: " + ", ".join(missing[:4]))
        # what the text states beyond the typed check (a negation, 'only', an order, a count, a name ...)
        extra, nums = unchecked_statements(kind, text)
        problems += extra
        names = unchecked_names(repo, kind, spec, ev, text, subjects)
        if names:
            problems.append(f"the text also names {', '.join(f'`{n}`' for n in names[:3])}, which the {kind} "
                            "check does not establish")
        problems += other_file_problems(kind, spec, ev, text, subjects)
        if kind == "location":
            name = spec.get("symbol") or next(iter(_BACKTICK.findall(text)), "")
            if name:
                problems += kind_problems(ev, _full, text, name, _a)
        if nums:
            read = _env_read_text(_full, spec["env"], int(_a), int(_b or _a)) \
                if kind == "config" and spec.get("env") and _full and _a and str(ev.get("path")).endswith(".py") \
                else None
            have = {_num(x) for x in re.findall(r"\d+(?:\.\d+)?", read or "")}
            off = [n for n in nums if _num(n) not in have]
            if off and read is not None:
                problems.append(f"the text states {', '.join(off[:3])}; the read at {ev.get('path')}:{_a} is "
                                f"`{' '.join(read.split())[:80]}`")
            elif off:
                problems.append(f"the text states a number ({', '.join(off[:3])}), which the {kind} check does "
                                "not compare")
        if problems:
            g = cap_grade(Grade(g.grade, g.reason + "; claim text: " + "; ".join(problems)), "partial")
    if spec.get("free_text") and kind == "general" and g.code == "quote":
        problem = _quote_rest_problem(repo, ev, text)
        if problem:
            g = Grade("partial", f"{g.reason}; {problem}", "quote_rest")
    if key is not None:
        if len(_CACHE) > 4096:
            _CACHE.clear()
        _CACHE[key] = g
    return g


def grade(kind: str, repo: Path | str | None, spec: dict | None, evidence_row: dict, *,
          text: str | None = None, subjects: list[str] | None = None) -> str:
    """``"full" | "partial" | "none"`` for one evidence row (the contract other modules build on)."""
    return assess(kind, repo, spec, evidence_row, text=text, subjects=subjects).grade
