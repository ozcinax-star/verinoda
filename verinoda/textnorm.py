"""Bilingual (Turkish/English) text normalisation shared by retrieval and question plans.

Design: docs/DESIGN.md D5. The folding is length-preserving (one character in,
one character out) so character offsets and line attribution survive it:

* ``fold_tr`` maps the Turkish letters to ASCII *before* lower-casing, so
  ``İndirim`` becomes ``indirim`` (plain ``str.lower`` yields ``i̇ndirim`` with a
  combining dot, which a word split then cuts into ``ndirim``).
* ``split_apostrophe`` separates Turkish case suffixes written after an
  apostrophe (``pricing.py'deki``, ``compute_total'ı``) so they never become
  search terms.
* ``tr_stem`` strips inflectional suffixes but only accepts a stem that the
  repository vocabulary confirms; otherwise it falls back to a 5-character
  prefix, which Can et al. (2008) found about as effective as lemmatisation
  for Turkish retrieval.

Question-understanding helpers (docs/DESIGN.md D2-D4) live here too, because
the lexicon, the question plan and retrieval must normalise identically:
``en_stem`` (light English suffix stripping), ``suffix_role`` (Turkish case
suffix -> grammatical role), ``is_suffix_chain`` (is a word remainder a run of
Turkish suffixes?), ``detect_language`` and ``ground_key`` (the canonical form
used to compare message text with plan text).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Callable, Iterable

_FOLD = str.maketrans({
    "İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ç": "c", "ç": "c", "Ğ": "g", "ğ": "g",
    "Ö": "o", "ö": "o", "Ü": "u", "ü": "u", "Â": "a", "â": "a", "Î": "i", "î": "i", "Û": "u", "û": "u",
})
_TR_LETTERS = set("çğıöşüÇĞİÖŞÜ")
APOSTROPHES = ("'", "’", "ʼ")

# Question and function words (folded). Never search terms.
TR_STOPWORDS = frozenset(
    """nasil nerede nereye nereden neresi hangi hangisi hangileri ne neyi neler nedir neden nicin niye
    kim kime kimi kimin mi mu ve veya ya yada ile icin gibi bu su o bunu sunu onu bunlar bunlari bir
    da de ki daha en cok var yok olan oluyor olur ediliyor yapiliyor yapar acaba peki lutfen goster
    anlat acikla nedir midir mudur hep her hangi sey seyi sekilde kadar gore ise ama fakat""".split()
)
EN_STOPWORDS = frozenset(
    """the a an and or of to in on at for from by with is are was were be been does do did how what
    which where why who when that this these those it its into about can could should would""".split()
)
# Short technical tokens that must survive minimum-length filters.
SHORT_TECH = frozenset({"db", "id", "io", "ui", "os", "ip", "ci", "vm", "js", "ts", "kv", "tx", "pr", "fs"})

# Turkish inflectional suffixes (folded), longest first. Plural, possessive, case,
# relative -ki, copula and common verb endings.
TR_SUFFIXES = sorted(set("""
lar ler lari leri larin lerin lara lere larda lerde lardan lerden
im in i si imiz iniz leri miz niz
nin nun in un yin yun
ni nu yi yu
ye ya e a ne na
de da te ta nde nda
den dan ten tan nden ndan
deki daki teki taki ndeki ndaki ki
le la yle yla ile
dir dur tir tur dir
mak mek mesi masi
iyor uyor yor iyorlar uyorlar
ecek acak yecek yacak
mis mus
di du ti tu
ir ur er ar
lik luk
ci cu
siz suz li lu
""".split()), key=len, reverse=True)

_WORD = re.compile(r"[\w]+", re.UNICODE)


def fold_tr(text: str) -> str:
    """Length-preserving Turkish-to-ASCII fold followed by lower-casing."""
    out = text.translate(_FOLD).lower()
    # str.lower() is length-preserving for everything except dotted capital I,
    # which the translation table already handled.
    return out


def has_turkish(text: str) -> bool:
    if any(ch in _TR_LETTERS for ch in text):
        return True
    words = {fold_tr(w) for w in _WORD.findall(text)}
    return len(words & TR_STOPWORDS) >= 2


def split_apostrophe(token: str) -> tuple[str, str]:
    """``"pricing.py'deki"`` -> ``("pricing.py", "deki")``; no apostrophe -> (token, "")."""
    for ap in APOSTROPHES:
        if ap in token:
            base, _, suffix = token.partition(ap)
            return base, suffix
    return token, ""


def raw_tokens(text: str) -> list[str]:
    """Whitespace tokens with apostrophe suffixes removed (case and letters kept)."""
    out = []
    for tok in text.split():
        base, _ = split_apostrophe(tok.strip(".,;:!?()[]{}\"`"))
        if base:
            out.append(base)
    return out


def words(text: str) -> list[str]:
    """Folded content words (apostrophe suffixes dropped, stopwords removed)."""
    out: list[str] = []
    for tok in raw_tokens(text):
        for w in _WORD.findall(fold_tr(tok)):
            if w in TR_STOPWORDS or w in EN_STOPWORDS:
                continue
            if len(w) < 3 and w not in SHORT_TECH:
                continue
            out.append(w)
    return out


def split_identifier(name: str) -> list[str]:
    """``getOrderRepo_v2`` -> ``['get', 'order', 'repo', 'v2']`` (folded)."""
    parts: list[str] = []
    for chunk in re.split(r"[_\W]+", name):
        if not chunk:
            continue
        parts += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+\d*|[A-Z]+\d*|\d+", chunk) or [chunk]
    return [fold_tr(p) for p in parts if p]


def tr_stem(word: str, vocab: Callable[[str], bool] | Iterable[str] | None = None,
            min_len: int = 3) -> str:
    """Strip Turkish suffixes, accepting only a stem confirmed by ``vocab``.

    ``vocab`` is a predicate (``prefix -> bool``) or a collection of known
    folded words/identifier parts; a candidate stem is accepted when some
    vocabulary word starts with it. Without confirmation the 5-character
    prefix is returned (or the word itself when it is short).
    """
    w = fold_tr(word)
    if vocab is None:
        known = None
    elif callable(vocab):
        known = vocab
    else:
        pool = tuple(vocab)
        known = lambda p: any(v.startswith(p) for v in pool)  # noqa: E731
    candidates = [w]
    cur = w
    changed = True
    while changed:
        changed = False
        for suf in TR_SUFFIXES:
            if cur.endswith(suf) and len(cur) - len(suf) >= min_len:
                cur = cur[: -len(suf)]
                candidates.append(cur)
                changed = True
                break
    if known is not None:
        for cand in candidates:  # longest first
            if known(cand):
                return cand
    return w[:5] if len(w) > 5 else w


# -- question-understanding helpers (D2-D4) ------------------------------------------------

# Turkish question words (folded): a message containing one is read with the
# Turkish cue tables even when it has no Turkish letters ("kayit nerede").
TR_QUESTION_WORDS = frozenset(
    """nasil nerede nereye nereden neresi hangi hangisi hangileri ne neyi neler nedir neden nicin
    niye kim kime kimi kimin mi mu midir mudur""".split()
) | frozenset(  # the question particle with a personal ending ("geçmeli miyiz", typed "gecmeli miyiz")
    "miyim miyiz misin misiniz muyum muyuz musun musunuz miydi muydu".split())

# Turkish case suffix (folded, after an apostrophe or stripped from a word) -> role.
TR_CASE_ROLES: dict[str, str] = {}
for _role, _sufs in (
    ("source", "den dan ten tan nden ndan"),
    ("target", "e a ye ya ne na"),
    ("container", "de da te ta nde nda deki daki teki taki ndeki ndaki"),
    ("owner", "in un nin nun"),
    ("object", "i u yi yu ni nu"),
    ("instrument", "le la yle yla ile"),
):
    for _s in _sufs.split():
        TR_CASE_ROLES[_s] = _role

# Suffixes a word remainder may be built from (inflection plus common verbal
# voice/tense/participle endings), used by :func:`is_suffix_chain`.
_CHAIN_SUFFIXES = tuple(sorted(set(TR_SUFFIXES) | set("""
il ul in un n l is us s t it ut dir tir dur tur ir ur ar er
iyor uyor yor abil ebil ma me mis mus di du ti tu ken ince unca arak erek ip up
an en yan yen dik duk tik tuk dig dug tig tug acak ecek yacak yecek
lik luk li lu siz suz ce ca da de ta te
u um su umuz unuz muz nuz
""".split()), key=len, reverse=True))

_EN_SUFFIXES = (("ations", 4), ("ation", 4), ("ments", 4), ("ment", 4), ("ings", 4), ("ing", 4),
                ("ies", 4), ("ers", 4), ("es", 4), ("ed", 3), ("er", 4), ("s", 3))


def nfc(text: str) -> str:
    """Unicode NFC (a decomposed ``I`` + combining dot becomes ``İ``)."""
    return unicodedata.normalize("NFC", text)


def ground_key(text: str) -> str:
    """Canonical form for "does this text occur verbatim in the message?".

    NFC, Turkish fold, case fold, every apostrophe variant unified to ``'``
    and runs of whitespace collapsed. Applied to both sides of a comparison.
    """
    t = nfc(text)
    for ap in APOSTROPHES[1:]:
        t = t.replace(ap, "'")
    return re.sub(r"\s+", " ", fold_tr(t).casefold()).strip()


def en_stem(word: str) -> str:
    """Light English suffix stripping for plain words (``variables`` -> ``variabl``)."""
    if len(word) < 5 or not word.isalpha():
        return word
    for suf, keep in _EN_SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= keep:
            return word[: -len(suf)]
    return word


def suffix_role(suffix: str) -> str | None:
    """Role of a Turkish case suffix (``"den"`` -> ``"source"``); longest known tail wins."""
    s = fold_tr(suffix)
    for n in range(len(s), 0, -1):
        role = TR_CASE_ROLES.get(s[-n:])
        if role:
            return role
    return None


@lru_cache(maxsize=8192)
def is_suffix_chain(rest: str, max_parts: int = 6) -> bool:
    """True when ``rest`` (folded) splits entirely into Turkish suffixes.

    ``is_suffix_chain("iliyor")`` (yaz+ıl+ıyor) is True, ``"ar"`` is True,
    ``"ilim"`` is True (il+im) - callers use it to accept a short dictionary
    stem only when the remainder of the word is inflection, not another word.
    """
    if rest == "":
        return True
    if max_parts <= 0:
        return False
    return any(rest.startswith(s) and is_suffix_chain(rest[len(s):], max_parts - 1)
               for s in _CHAIN_SUFFIXES)


def tr_stem_candidates(word: str, min_len: int = 3) -> list[str]:
    """Every stem :func:`tr_stem` considers, longest first (the folded word included)."""
    cur = fold_tr(word)
    out = [cur]
    changed = True
    while changed:
        changed = False
        for suf in TR_SUFFIXES:
            if cur.endswith(suf) and len(cur) - len(suf) >= min_len:
                cur = cur[: -len(suf)]
                out.append(cur)
                changed = True
                break
    return out


def detect_language(text: str) -> str:
    """``"tr"``, ``"en"``, ``"mixed"`` or ``"other"`` from letters and function words."""
    toks = [fold_tr(w) for w in _WORD.findall(text)]
    if not toks:
        return "other"
    tr_letters = any(ch in _TR_LETTERS for ch in text)
    tr_words = sum(1 for w in toks if w in TR_STOPWORDS or w in TR_QUESTION_WORDS)
    en_words = sum(1 for w in toks if w in EN_STOPWORDS)
    is_tr = tr_letters or tr_words >= 2 or (tr_words >= 1 and en_words == 0)
    is_en = en_words >= 2 or (en_words >= 1 and not is_tr)
    if is_tr and is_en:
        return "mixed"
    if is_tr:
        return "tr"
    if is_en or all(t.isascii() for t in toks):
        return "en"
    return "other"
