"""Repo-learned lexicon: natural-language words -> identifier parts (docs/DESIGN.md D6).

Users ask with the words of their domain ("sipariş", "persistence", "cache
entry"); code names things with identifiers (``OrderRepository.save``,
``_cache_key``). This module learns which words the *repository itself* uses
next to which identifier parts, so a question can reach code whose names it
does not spell. It never produces evidence for a claim - only candidates that
the question plan grounds in the graph and labels ``derived_by=lexicon`` or
``seed_dictionary``.

**Units.** One unit per definition (Python: every ``def``/``class`` via
``ast``; other languages: every graph symbol with a span) plus one per module
and one per documentation line that contains a backticked identifier.

* Natural-language words come from docstrings, comments (Python via
  ``tokenize``; other languages via ``#``/``//``/``--``/``/* */`` regexes) and
  string literals inside the unit, and from the prose around backticked
  identifiers in docs.
* Identifier parts come from the unit's own name (weight 1.0) and its
  enclosing class (0.5). Parameters are recorded (0.3) but not associated, and
  names of callees are excluded: on graphify_core parameter names (``question``,
  ``mode``) produced most of the noise, as callee names did in the prototype.
  Units in test files feed the vocabulary but neither the association nor the
  text-hit index - test names restate the words of the code they test.
* Words are folded (:func:`verinoda.textnorm.fold_tr`) and keyed by
  :func:`word_keys` - the light English stem, or the 5-character prefix for
  Turkish words (Can et al. 2008).

**Association.** Dunning's log-likelihood G² over the unit x (word, part)
contingency table; a pair is kept when G² >= 10.83 (p < 0.001), the
association is positive and at least two distinct units support it (identical
units, e.g. a doc line copied into several files, count once). ``score =
min(1, G²/50)``. Parts in the :data:`PART_STOPLIST` and parts that occur in
more than 5% of the units of a large repository are dropped. At most 20 pairs
per word, each with up to 3 ``file:line`` sites so a reader can audit it.

**Vocabulary.** The file also records, per folded identifier part or word,
how many files contain it. The question plan uses it to keep a seed-dictionary
expansion only when its English target occurs in the repository, and analysis
uses it as an idf table for relevance.

**Seed dictionary.** ``verinoda/data/seed_lexicon_tr_en.json`` maps about
four hundred Turkish stems of generic software, business and game/mod
development terms to English words (``veritaban`` -> database, ``siparis`` ->
order, ``esya`` -> item). Stems that folding merges with another word (``öl``
die / ``ol`` be) live in a separate ``exact`` section and match only the word as
written (:func:`seed_exact_lookup`: "öldüğünde" but not "oluyor"). A short key only
matches when the rest of the word is Turkish inflection
(:func:`verinoda.textnorm.is_suffix_chain`), and the longest matching key
wins (``indirim`` -> discount, not ``indir`` -> download).

**Translations.** A repository that ships the same strings in two languages
(a Minecraft ``lang/en_us.json`` next to ``lang/tr_tr.json``, i18n
``locales/en.json`` next to ``locales/tr.json``) states its own dictionary:
for every key present in both files, each Turkish word of the value is paired
with the English word at the same position when the two values have the same
number of words ("Fener Asası" / "Lantern Staff": fener -> lantern, asası ->
staff), and the whole Turkish value with the key's last identifier
(``lantern_staff``). These pairs are kept with ``"via": "translation"`` and the
line of the Turkish file as their site; like every lexicon pair they only widen
the search.

The file is ``.verinoda/index/lexicon.json``. :func:`build` is incremental:
files whose sha256 did not change keep their extracted units.
"""

from __future__ import annotations

import ast
import bisect
import io
import json
import math
import re
import time
import tokenize
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path, PurePosixPath

from verinoda import textnorm as tn

LEXICON_VERSION = 1
FILE_NAME = "lexicon.json"
G2_MIN = 10.83            # chi-square(1) at p < 0.001
MIN_SUPPORT = 2           # units that must contain the pair
SCORE_SCALE = 50.0        # score = min(1, G2 / SCORE_SCALE)
TOP_PAIRS = 20            # pairs kept per word
MAX_SITES = 3             # evidence sites kept per pair
MAX_TEXT_SITES = 5        # sites kept per word for text hits
COMMON_PART_SHARE = 0.05  # parts in more than this share of units are dropped ...
COMMON_PART_MIN_UNITS = 100  # ... once the repository has at least this many units
MAX_FILE_BYTES = 1_000_000
WEIGHT_OWN, WEIGHT_CLASS, WEIGHT_PARAM = 1.0, 0.5, 0.3

PART_STOPLIST = frozenset("""
get set add len str int list dict init self cls args kwargs value data item obj tmp util utils helper
helpers main run new none true false the and for from with test tests all any has can make one two old
via per raw val var ret res out
""".split())
NL_STOPLIST = frozenset("""
returns return none true false self cls args kwargs str int dict list bool tuple float optional
param params arg type types default see note todo fixme xxx also used use uses using given
""".split())
CODE_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
                 ".kt", ".kts", ".scala", ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp",
                 ".swift", ".lua", ".sh", ".bash", ".ps1", ".sql", ".ex", ".exs", ".jl", ".zig", ".m",
                 ".groovy", ".dart", ".vue", ".svelte"}
DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
# Endings that keep a word's meaning: ``persist`` is in the vocabulary through ``persistence``.
DERIVATIONAL = frozenset("""
s es ed d er ers ing ings ion ions ation ations ment ments ence ences ance ances ity ities al ally ly
e ure ures uration urations ive able ability or ors ory ize izes ized ization ise ised isation
""".split())
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NL_WORD = re.compile(r"[^\W\d_]{3,}")
_BACKTICK = re.compile(r"`([A-Za-z_][\w.:/-]*)(?:\(\))?`")
_LINE_COMMENT = re.compile(r"(?:^|\s)(?:#|//|--)\s?(.*)$")
_BLOCK_COMMENT = re.compile(r"/\*(.*?)\*/")
_STRING = re.compile(r"\"([^\"\\\n]{3,})\"|'([^'\\\n]{3,})'|`([^`\\\n]{3,})`")


# -- normalisation ----------------------------------------------------------------------------

def word_keys(word: str) -> list[str]:
    """Lookup keys of a natural-language word: English stem, then 5-char prefix (Turkish)."""
    w = tn.fold_tr(word)
    keys = [tn.en_stem(w)]
    if len(w) > 5 and w[:5] not in keys:
        keys.append(w[:5])
    return keys


def _nl_key(word: str, turkish: bool) -> str:
    w = tn.fold_tr(word)
    if turkish:
        return w[:5] if len(w) > 5 else w
    return tn.en_stem(w)


@lru_cache(maxsize=65536)
def ident_parts(name: str) -> tuple[str, ...]:
    """Folded identifier parts worth indexing (3+ characters or a short technical term)."""
    return tuple(p for p in tn.split_identifier(name)
                 if (len(p) >= 3 or p in tn.SHORT_TECH) and not p.isdigit())


def _nl_words(text: str) -> list[tuple[str, bool]]:
    """(word, is_turkish) for the content words of a comment/docstring/string."""
    if not text:
        return []
    turkish_text = tn.has_turkish(text)
    out = []
    for tok in tn.raw_tokens(text):
        for w in _NL_WORD.findall(tok):
            f = tn.fold_tr(w)
            if f in tn.TR_STOPWORDS or f in tn.EN_STOPWORDS or f in NL_STOPLIST:
                continue
            out.append((w, turkish_text or any(ch in "çğıöşüÇĞİÖŞÜ" for ch in w)))
    return out


# -- per-file extraction ----------------------------------------------------------------------

@dataclass
class _Unit:
    site: str
    words: set[str] = field(default_factory=set)       # NL word keys
    parts: dict[str, float] = field(default_factory=dict)  # identifier part -> weight
    body: set[str] = field(default_factory=set)        # identifier parts used in the body

    def add_parts(self, name: str, weight: float) -> None:
        for p in ident_parts(name):
            if weight > self.parts.get(p, 0.0):
                self.parts[p] = weight

    def add_text(self, text: str) -> None:
        for w, turkish in _nl_words(text):
            self.words.add(_nl_key(w, turkish))

    def pack(self) -> list:
        return [self.site, sorted(self.words), {k: self.parts[k] for k in sorted(self.parts)},
                sorted(self.body)]


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _own_nodes(node: ast.AST):
    """Nodes of ``node``'s body that are not inside a nested definition."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        if isinstance(n, _DEFS):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _docstring_node(node: ast.AST) -> ast.AST | None:
    body = getattr(node, "body", None)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0].value
    return None


def _fill_own(u: _Unit, node: ast.AST) -> None:
    """Strings, names and attributes of ``node``'s own body (docstring excluded)."""
    doc = _docstring_node(node)
    for sub in _own_nodes(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub is not doc:
            if not sub.value.isupper():
                u.add_text(sub.value)
        elif isinstance(sub, ast.Name):
            u.body.update(ident_parts(sub.id))
        elif isinstance(sub, ast.Attribute):
            u.body.update(ident_parts(sub.attr))
    u.body -= set(u.parts)


def _py_units(rel: str, src: str) -> list[_Unit]:
    tree = ast.parse(src)
    comments: dict[int, list[str]] = defaultdict(list)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]].append(tok.string.lstrip("#"))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    mod = _Unit(f"{rel}:1")
    mod.add_parts(PurePosixPath(rel).stem, WEIGHT_OWN)
    mod.add_text(ast.get_docstring(tree) or "")
    units: list[_Unit] = [mod]
    spans: list[tuple[int, int, int]] = []  # (first line, end line, unit index)

    def visit(node: ast.AST, cls: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _DEFS):
                visit(child, cls)
                continue
            u = _Unit(f"{rel}:{child.lineno}")
            u.add_parts(child.name, WEIGHT_OWN)
            if cls:
                u.add_parts(cls, WEIGHT_CLASS)
            if not isinstance(child, ast.ClassDef):
                a = child.args
                for arg in a.posonlyargs + a.args + a.kwonlyargs:
                    if arg.arg not in ("self", "cls"):
                        u.add_parts(arg.arg, WEIGHT_PARAM)
            u.add_text(ast.get_docstring(child) or "")
            _fill_own(u, child)
            first = min([child.lineno] + [d.lineno for d in child.decorator_list])
            spans.append((first, getattr(child, "end_lineno", None) or child.lineno, len(units)))
            units.append(u)
            visit(child, child.name if isinstance(child, ast.ClassDef) else cls)

    visit(tree, None)
    # A comment belongs to the innermost definition around it, or to the one
    # starting within the next two lines (a leading comment), else to the module.
    for ln, texts in comments.items():
        inside = [s for s in spans if s[0] <= ln <= s[1]]
        if inside:
            owner = max(inside, key=lambda s: s[0])[2]
        else:
            lead = [s for s in spans if 0 < s[0] - ln <= 2]
            owner = min(lead, key=lambda s: s[0])[2] if lead else 0
        for c in texts:
            units[owner].add_text(c)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    mod.body.update(ident_parts(sub.id))
    mod.body -= set(mod.parts)
    return units


def _generic_units(rel: str, lines: list[str], graph) -> list[_Unit]:
    """Units of a non-Python code file from the graph's symbols and their spans."""
    if graph is None:
        return []
    units: list[_Unit] = []
    for nid in graph.symbols_in(rel):
        sp = graph.span(nid)
        if not sp:
            continue
        u = _Unit(f"{rel}:{sp[0]}")
        label = graph.label(nid).strip().lstrip(".").split("(")[0]
        u.add_parts(label, WEIGHT_OWN)
        cls = next((c for c, _ in graph.in_edges(nid, {"method"}) if graph.file(c) == rel), None)
        if cls:
            u.add_parts(graph.label(cls), WEIGHT_CLASS)
        for i in range(sp[0], min(sp[1], len(lines)) + 1):
            if graph.symbol_at(rel, i) not in (nid, None):
                continue  # a nested symbol's own lines belong to it
            line = lines[i - 1]
            m = _LINE_COMMENT.search(line)
            if m:
                u.add_text(m.group(1))
            for bm in _BLOCK_COMMENT.finditer(line):
                u.add_text(bm.group(1))
            for sm in _STRING.finditer(line):
                s = next(g for g in sm.groups() if g)
                if not s.isupper():
                    u.add_text(s)
            for ident in _IDENT.findall(line):
                u.body.update(ident_parts(ident))
        u.body -= set(u.parts)
        units.append(u)
    return units


def _doc_units(rel: str, lines: list[str]) -> list[_Unit]:
    units = []
    for i, line in enumerate(lines, 1):
        names = _BACKTICK.findall(line)
        if not names:
            continue
        u = _Unit(f"{rel}:{i}")
        for n in names:
            u.add_parts(re.split(r"[./:]", n.rstrip("/"))[-1] or n, WEIGHT_OWN)
        u.add_text(_BACKTICK.sub(" ", line))
        if u.words and u.parts:
            units.append(u)
    return units


def _extract(repo: Path, rel: str, graph) -> dict:
    """Units and vocabulary of one file (the unit of incremental rebuilds)."""
    p = repo / rel
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return {"units": [], "vocab": []}
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"units": [], "vocab": []}
    suffix = PurePosixPath(rel).suffix.lower()
    lines = text.splitlines()
    units: list[_Unit] = []
    if suffix in (".py", ".pyi"):
        try:
            units = _py_units(rel, text)
        except (SyntaxError, ValueError, RecursionError):
            units = _generic_units(rel, lines, graph)
    elif suffix in DOC_SUFFIXES:
        units = _doc_units(rel, lines)
    elif suffix in CODE_SUFFIXES:
        units = _generic_units(rel, lines, graph)
    vocab: set[str] = set()
    if suffix in DOC_SUFFIXES:
        vocab.update(tn.fold_tr(w) for w, _ in _nl_words(text))
        for n in _BACKTICK.findall(text):
            vocab.update(ident_parts(n.replace(".", "_").replace("/", "_")))
    else:
        for ident in set(_IDENT.findall(text)):
            vocab.update(ident_parts(ident))
        for u in units:
            vocab.update(u.words)
    vocab.update(ident_parts(PurePosixPath(rel).stem))
    for part in PurePosixPath(rel).parts[:-1]:
        vocab.update(ident_parts(part))
    return {"units": [u.pack() for u in units if u.parts],
            "vocab": sorted(v for v in vocab if v)}


# -- association ------------------------------------------------------------------------------

def g2(k11: float, k1_: float, k_1: float, n: float) -> float:
    """Dunning's log-likelihood ratio for a 2x2 table given marginals."""
    k12, k21 = k1_ - k11, k_1 - k11
    k22 = n - k11 - k12 - k21

    def h(*ks: float) -> float:
        s = sum(ks)
        return sum(k * math.log(k / s) for k in ks if k > 0)

    return max(0.0, 2 * (h(k11, k12, k21, k22) - h(k11 + k12, k21 + k22) - h(k11 + k21, k12 + k22)))


def _associate(units: list[list]) -> tuple[dict, dict, list[str], int]:
    from verinoda.architecture_map import is_test_file

    # Identical units (the same doc line copied into many files) count once.
    seen: set[tuple] = set()
    kept = []
    for u in units:
        if is_test_file(u[0].rpartition(":")[0]):
            continue
        sig = (tuple(u[1]), tuple(sorted(u[2])))
        if sig not in seen:
            seen.add(sig)
            kept.append(u)
    units = kept
    n = len(units)
    unit_df: Counter = Counter()
    for u in units:
        unit_df.update(u[2].keys())
    stop = set(PART_STOPLIST)
    if n >= COMMON_PART_MIN_UNITS:
        stop |= {p for p, c in unit_df.items() if c > COMMON_PART_SHARE * n}
    cw: Counter = Counter()
    ci: Counter = Counter()
    cwi: Counter = Counter()
    sup: Counter = Counter()
    text_sites: dict[str, list[str]] = defaultdict(list)
    kept_parts = []
    for site, words, parts, body in units:
        parts = {p: w for p, w in parts.items() if p not in stop and w >= WEIGHT_CLASS}
        kept_parts.append(parts)
        cw.update(words)
        for p, w in parts.items():
            ci[p] += w
        for word in words:
            if len(text_sites[word]) < MAX_TEXT_SITES:
                text_sites[word].append(site)
            for p, w in parts.items():
                if p == word or tn.en_stem(p) == word:
                    continue
                cwi[(word, p)] += w
                sup[(word, p)] += 1
        for b in body:
            if len(text_sites[b]) < MAX_TEXT_SITES and site not in text_sites[b]:
                text_sites[b].append(site)
    pairs: dict[str, list[dict]] = defaultdict(list)
    for (word, p), k in cwi.items():
        if sup[(word, p)] < MIN_SUPPORT:
            continue
        if k / cw[word] <= ci[p] / n:  # not a positive association
            continue
        s = g2(k, cw[word], ci[p], n)
        if s >= G2_MIN:
            pairs[word].append({"part": p, "g2": round(s, 2), "support": sup[(word, p)],
                                "score": round(min(1.0, s / SCORE_SCALE), 3), "sites": []})
    wanted: dict[tuple[str, str], dict] = {}
    for word in pairs:
        pairs[word] = sorted(pairs[word], key=lambda x: (-x["g2"], x["part"]))[:TOP_PAIRS]
        for pr in pairs[word]:
            wanted[(word, pr["part"])] = pr
    # second pass: evidence sites for the pairs that were kept
    for (site, words, _, _), parts in zip(units, kept_parts):
        for word in words:
            for p in parts:
                pr = wanted.get((word, p))
                if pr is not None and len(pr["sites"]) < MAX_SITES:
                    pr["sites"].append(site)
    return dict(sorted(pairs.items())), dict(sorted(text_sites.items())), sorted(stop), n


# -- translations: parallel locale files ---------------------------------------------------------

_LOCALE_FILE = re.compile(r"(?:^|/)(?:lang|langs|locale|locales|i18n|l10n|translations?)/"
                          r"(en|en_us|en-us|en_gb|tr|tr_tr|tr-tr)\.json$", re.I)
TRANSLATION_SCORE = 0.9
MAX_TRANSLATION_WORDS = 6


def _flat_strings(obj, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, str):
                out[key] = v
            elif isinstance(v, dict):
                out.update(_flat_strings(v, key))
    return out


def _value_words(text: str) -> list[str]:
    text = re.sub(r"%\w|\{[^}]*\}|§.|<[^>]+>", " ", text)
    return re.findall(r"[^\W\d_]+", text)


def _locale_entries(repo: Path, files: list[str]):
    """``(site, key, tr_value, en_value)`` for the keys an en and a tr locale file both translate."""
    groups: dict[str, dict[str, str]] = defaultdict(dict)
    for rel in files:
        m = _LOCALE_FILE.search(rel)
        if m:
            lang = "tr" if m.group(1).lower().startswith("tr") else "en"
            groups[rel[:m.start(1)]][lang] = rel
    for pair in groups.values():
        if "en" not in pair or "tr" not in pair:
            continue
        try:
            en_raw = (repo / pair["en"]).read_text(encoding="utf-8", errors="replace")
            tr_raw = (repo / pair["tr"]).read_text(encoding="utf-8", errors="replace")
            en, tr = _flat_strings(json.loads(en_raw)), _flat_strings(json.loads(tr_raw))
        except (OSError, ValueError):
            continue
        tr_lines = tr_raw.splitlines()
        for key, tr_val in tr.items():
            en_val = en.get(key)
            if not isinstance(en_val, str) or tn.fold_tr(en_val) == tn.fold_tr(tr_val):
                continue
            leaf = key.rsplit(".", 1)[-1]
            line = next((i for i, ln in enumerate(tr_lines, 1) if f'"{leaf}"' in ln or f'"{key}"' in ln), 1)
            yield f"{pair['tr']}:{line}", key, tr_val, en_val


def _is_leaf_identifier(leaf: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{2,}", leaf)) and "_" in leaf


def translation_pairs(repo: Path, files: list[str]) -> dict[str, list[dict]]:
    """Turkish word key -> ``[{part, score, support, sites, via}]`` from parallel en/tr locale files."""
    found: dict[tuple[str, str], dict] = {}
    for site, key, tr_val, en_val in _locale_entries(repo, files):
        leaf = key.rsplit(".", 1)[-1]
        tw, ew = _value_words(tr_val), _value_words(en_val)
        if not tw or len(tw) > MAX_TRANSLATION_WORDS:
            continue
        links: list[tuple[str, str]] = []
        if len(tw) == len(ew):
            links += [(a, b) for a, b in zip(tw, ew) if tn.fold_tr(a) != tn.fold_tr(b)]
        if _is_leaf_identifier(leaf):
            links += [(a, leaf) for a in tw]
        for tr_word, target in links:
            if len(tr_word) < 3 or len(target) < 3 or tn.fold_tr(tr_word) in tn.TR_STOPWORDS:
                continue
            part = tn.fold_tr(target) if "_" in target else tn.en_stem(tn.fold_tr(target))
            for k in word_keys(tr_word):
                e = found.setdefault((k, part), {"part": part, "score": TRANSLATION_SCORE, "support": 0,
                                                 "sites": [], "via": "translation"})
                e["support"] += 1
                if len(e["sites"]) < MAX_SITES and site not in e["sites"]:
                    e["sites"].append(site)
    out: dict[str, list[dict]] = defaultdict(list)
    for (k, _part), e in found.items():
        out[k].append(e)
    return {k: sorted(v, key=lambda x: (-x["support"], x["part"]))[:TOP_PAIRS] for k, v in sorted(out.items())}


def translation_phrases(repo: Path, files: list[str]) -> dict[str, dict]:
    """Folded Turkish label of two or more words -> ``{targets, sites}``: the identifiers its keys end in.

    "Kor Ocağı" under ``block.emberforge.ember_forge`` -> ``ember_forge``: a question that names a
    thing by its Turkish label names that identifier, not every word the label's words occur in.
    """
    out: dict[str, dict] = {}
    for site, key, tr_val, _en_val in _locale_entries(repo, files):
        leaf = key.rsplit(".", 1)[-1]
        tw = _value_words(tr_val)
        if not _is_leaf_identifier(leaf) or not 2 <= len(tw) <= MAX_TRANSLATION_WORDS:
            continue
        e = out.setdefault(" ".join(tn.fold_tr(w).lower() for w in tw), {"targets": [], "sites": []})
        if leaf not in e["targets"] and len(e["targets"]) < TOP_PAIRS:
            e["targets"].append(leaf)
        if site not in e["sites"] and len(e["sites"]) < MAX_SITES:
            e["sites"].append(site)
    return dict(sorted(out.items()))


# -- seed dictionary --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def seed_entries() -> dict[str, tuple[str, ...]]:
    """The packaged TR->EN seed dictionary: folded Turkish key -> English words."""
    raw = resources.files("verinoda").joinpath("data/seed_lexicon_tr_en.json").read_text(encoding="utf-8")
    return {k: tuple(v) for k, v in json.loads(raw)["entries"].items()}


@lru_cache(maxsize=1)
def seed_exact_entries() -> dict[str, tuple[str, ...]]:
    """Seed entries keyed by the Turkish spelling (``öl``): stems folding would merge with another word."""
    raw = resources.files("verinoda").joinpath("data/seed_lexicon_tr_en.json").read_text(encoding="utf-8")
    return {k: tuple(v) for k, v in (json.loads(raw).get("exact") or {}).items()}


def tr_lower(word: str) -> str:
    """Lowercase with Turkish dotted/dotless i, keeping the other Turkish letters."""
    return tn.nfc(word).replace("İ", "i").replace("I", "ı").lower()


def seed_exact_lookup(originals: list[str]) -> list[tuple[int, str, tuple[str, ...]]]:
    """``(index, key, english)`` for words as written that are an exact key plus Turkish inflection."""
    entries = seed_exact_entries()
    out = []
    for i, w in enumerate(originals):
        low = tr_lower(w)
        # the longest key the word starts with decides: "ölçülüyor" is ölç (measure) even where
        # its ending is not read as inflection, never öl (die)
        key = max((k for k in entries if low.startswith(k)), key=len, default=None)
        if key and (low == key or tn.is_suffix_chain(tn.fold_tr(low[len(key):]))):
            out.append((i, key, entries[key]))
    return out


# Final-consonant softening before a vowel-initial suffix, folded: reddet -> reddediyor,
# kaydet -> kaydedilir, gerek -> gereği (ğ folds to g), kitap -> kitabı. ç -> c is
# invisible after folding.
_SOFTENED = {"t": "d", "k": "g", "p": "b"}
_VOWELS = frozenset("aeiou")


def seed_key_matches(key_word: str, word: str) -> bool:
    """Does the folded ``word`` start with seed key word ``key_word`` followed only by inflection?

    The key's last consonant may be softened (``reddet`` matches ``reddediyor``) when
    a vowel-initial suffix follows it.
    """
    if word.startswith(key_word):
        rest = word[len(key_word):]
        return len(key_word) >= 5 or tn.is_suffix_chain(rest)
    soft = _SOFTENED.get(key_word[-1:])
    if soft is None or len(key_word) < 2:
        return False
    stem = key_word[:-1] + soft
    rest = word[len(stem):]
    if not word.startswith(stem) or not rest or rest[0] not in _VOWELS:
        return False
    return len(key_word) >= 5 or tn.is_suffix_chain(rest)


def seed_lookup(words: list[str]) -> list[tuple[int, int, str, tuple[str, ...]]]:
    """Seed entries found in a list of folded words: ``(start, n_words, key, english)``.

    Multi-word keys are matched on adjacent words; per start position the key
    covering the most words wins, then the longest key.
    """
    entries = seed_entries()
    found = []
    for i in range(len(words)):
        best = None
        for key, targets in entries.items():
            kw = key.split()
            if i + len(kw) > len(words):
                continue
            if all(seed_key_matches(k, words[i + j]) for j, k in enumerate(kw)):
                cand = (len(kw), len(key), key, targets)
                if best is None or cand[:2] > best[:2]:
                    best = cand
        if best:
            found.append((i, best[0], best[2], best[3]))
    return found


# -- the loaded lexicon -----------------------------------------------------------------------

@dataclass
class Lexicon:
    """A loaded (or graph-derived) lexicon. See the module docstring."""

    pairs: dict[str, list[dict]] = field(default_factory=dict)
    vocab: dict[str, int] = field(default_factory=dict)
    n_files: int = 0
    text_sites: dict[str, list[str]] = field(default_factory=dict)
    units: int = 0
    tree_hash: str | None = None
    built_at: str | None = None
    source: str = "file"
    phrases: dict[str, dict] = field(default_factory=dict)
    _sorted: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._sorted = sorted(self.vocab)

    # vocabulary
    def _prefixed(self, prefix: str) -> list[str]:
        i = bisect.bisect_left(self._sorted, prefix)
        out = []
        while i < len(self._sorted) and self._sorted[i].startswith(prefix) and len(out) < 64:
            out.append(self._sorted[i])
            i += 1
        return out

    def has(self, term: str) -> bool:
        """Does the repository use ``term`` (folded) as a word or identifier part?"""
        return self.df(term) > 0

    def df(self, term: str) -> int:
        """Files containing ``term``: exact, or through its stem (``orders`` ~ ``order``)."""
        t = tn.fold_tr(term)
        if t in self.vocab:
            return self.vocab[t]
        st = tn.en_stem(t)
        best = self.vocab.get(st, 0)
        if len(st) >= 4:
            for v in self._prefixed(st):
                if tn.en_stem(v) == st or v[len(st):] in DERIVATIONAL:
                    best = max(best, self.vocab[v])
        return best

    def idf(self, term: str) -> float:
        n = max(1, self.n_files)
        d = self.df(term)
        return math.log(1 + (n - d + 0.5) / (d + 0.5))

    def grounded(self, english: str) -> bool:
        """Is a seed target (``trial_balance`` counts part by part) present in this repository?"""
        parts = [p for p in tn.split_identifier(english) if len(p) >= 2]
        return bool(parts) and all(self.has(p) for p in parts)

    # expansions
    def associations(self, word: str) -> list[dict]:
        """Repo-learned pairs for a word (best first)."""
        out: dict[str, dict] = {}
        for key in word_keys(word):
            for pr in self.pairs.get(key, ()):
                if pr["part"] not in out or pr["score"] > out[pr["part"]]["score"]:
                    out[pr["part"]] = {**pr, "key": key}
        return sorted(out.values(), key=lambda x: (-x["score"], x["part"]))

    def seed(self, words: list[str]) -> list[dict]:
        """Grounded seed expansions for folded words: ``{start, n, key, targets, dropped}``."""
        res = []
        for start, n, key, targets in seed_lookup(words):
            kept = [t for t in targets if self.grounded(t)]
            res.append({"start": start, "n": n, "key": key, "targets": kept,
                        "dropped": [t for t in targets if t not in kept]})
        return res

    def phrase_hits(self, words: list[str]) -> list[dict]:
        """Locale labels spelled by adjacent folded words: ``{start, n, key, targets, sites}``.

        Each label word may carry Turkish inflection (``kor ocağının``); per start the longest label wins.
        """
        res: list[dict] = []
        i = 0
        while i < len(words) and self.phrases:
            best = None
            for key, e in self.phrases.items():
                kw = key.split()
                if len(kw) > len(words) - i or (best and len(kw) <= best[0]):
                    continue
                if all(seed_key_matches(k, words[i + j]) for j, k in enumerate(kw)):
                    best = (len(kw), key, e)
            if best is None:
                i += 1
                continue
            res.append({"start": i, "n": best[0], "key": best[1], "targets": list(best[2].get("targets") or []),
                        "sites": list(best[2].get("sites") or [])})
            i += best[0]
        return res

    def seed_exact(self, originals: list[str]) -> list[dict]:
        """Grounded exact-spelling seed hits for the words as written: ``{index, key, targets}``."""
        return [{"index": i, "key": key, "targets": kept}
                for i, key, targets in seed_exact_lookup(originals)
                if (kept := [x for x in targets if self.grounded(x)])]

    def text_hits(self, word: str) -> list[str]:
        """``file:line`` sites of units whose comments/strings/body use the word."""
        out: list[str] = []
        for key in dict.fromkeys(word_keys(word) + [tn.fold_tr(word)]):
            for s in self.text_sites.get(key, ()):
                if s not in out:
                    out.append(s)
        return out[:MAX_TEXT_SITES]

    def summary(self) -> dict:
        return {"source": self.source, "units": self.units, "words": len(self.pairs),
                "pairs": sum(len(v) for v in self.pairs.values()), "vocab": len(self.vocab),
                "files": self.n_files, "tree_hash": self.tree_hash}


def from_graph(graph) -> Lexicon:
    """A lexicon with only a vocabulary (graph labels and paths): no pairs, no text hits.

    Used when ``lexicon.json`` has not been built; seed expansions are still
    grounded, against the names the graph knows.
    """
    vocab: Counter = Counter()
    files = set()
    for n, d in graph.G.nodes(data=True):
        f = d.get("source_file")
        if not f:
            continue
        files.add(f)
        for p in ident_parts(str(d.get("label") or "").replace(".", "_")):
            vocab[p] += 1
    for f in files:
        for part in PurePosixPath(f).parts:
            for p in ident_parts(PurePosixPath(part).stem):
                vocab[p] += 1
    return Lexicon(vocab=dict(vocab), n_files=len(files), source="graph")


# -- persistence ------------------------------------------------------------------------------

def lexicon_path(repo: Path) -> Path:
    from verinoda.paths import index_dir

    return index_dir(Path(repo)) / FILE_NAME


def _read_raw(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("version") == LEXICON_VERSION else None


def _from_raw(raw: dict) -> Lexicon:
    return Lexicon(pairs=raw.get("pairs") or {}, vocab=raw.get("vocab") or {}, phrases=raw.get("phrases") or {},
                   n_files=int(raw.get("n_files") or 0), text_sites=raw.get("text_sites") or {},
                   units=int(raw.get("units") or 0), tree_hash=raw.get("tree_hash"),
                   built_at=raw.get("built_at"), source="file")


_LOADED: dict[str, tuple[tuple[int, int], Lexicon | None]] = {}


def load(repo: Path) -> Lexicon | None:
    """The lexicon built for ``repo``, or None when there is none (or it is from another version).

    Parsed once per file version (modification time and size): one question reads it several
    times, and a large repository's lexicon takes a noticeable fraction of a second to parse.
    """
    path = lexicon_path(repo)
    try:
        st = path.stat()
    except OSError:
        return None
    key, stamp = str(path), (st.st_mtime_ns, st.st_size)
    hit = _LOADED.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    raw = _read_raw(path)
    lx = _from_raw(raw) if raw else None
    if len(_LOADED) >= 4:
        _LOADED.clear()
    _LOADED[key] = (stamp, lx)
    return lx


def _candidate_files(repo: Path, graph) -> list[str]:
    if graph is not None:
        files = {d["source_file"] for _, d in graph.G.nodes(data=True) if d.get("source_file")}
    else:
        from verinoda.snapshot import list_files

        files = set(list_files(repo))
    return sorted(f for f in files if PurePosixPath(f).suffix.lower() in CODE_SUFFIXES | DOC_SUFFIXES)


def _norm_rel(repo: Path, p) -> str:
    q = Path(p)
    if q.is_absolute():
        try:
            return q.resolve().relative_to(repo).as_posix()
        except ValueError:
            return q.as_posix()
    return PurePosixPath(str(p).replace("\\", "/")).as_posix()


def build(repo: Path, graph=None, changed=None, *, file_hashes: dict[str, str] | None = None,
          tree_hash: str | None = None) -> dict:
    """(Re)build ``.verinoda/index/lexicon.json``; returns build statistics.

    ``graph`` (an :class:`verinoda.index.Graph`) supplies the file list and
    the symbol spans of non-Python files; without it the tracked files are
    listed and only Python and docs yield units. ``changed`` (paths, absolute
    or repository-relative) limits re-extraction to those files; files not
    listed keep their stored units. Without ``changed`` every file whose
    sha256 differs from the stored one is re-extracted (``file_hashes``, e.g.
    a snapshot's, spares re-hashing). Never raises for a single bad file.
    """
    from verinoda.snapshot import sha256_file
    from verinoda.store import now

    t0 = time.monotonic()
    repo = Path(repo).resolve()
    path = lexicon_path(repo)
    old = _read_raw(path) or {}
    old_files: dict = old.get("files") or {}
    changed_set = None if changed is None else {_norm_rel(repo, c) for c in changed}
    files: dict[str, dict] = {}
    reparsed = 0
    for rel in _candidate_files(repo, graph):
        prev = old_files.get(rel)
        if changed_set is not None and prev is not None and rel not in changed_set:
            files[rel] = prev
            continue
        sha = (file_hashes or {}).get(rel)
        if sha is None:
            try:
                sha = sha256_file(repo / rel)
            except OSError:
                continue
        if prev is not None and prev.get("sha256") == sha:
            files[rel] = prev
            continue
        files[rel] = {"sha256": sha, **_extract(repo, rel, graph)}
        reparsed += 1
    units = [u for f in files.values() for u in f.get("units", ())]
    pairs, text_sites, stop, n_units = _associate(units)
    try:
        from verinoda.snapshot import list_files

        listed = list_files(repo)
        translations = translation_pairs(repo, listed)
        phrases = translation_phrases(repo, listed)
    except Exception:  # noqa: BLE001 - a malformed locale file never breaks the lexicon
        translations, phrases = {}, {}
    for k, prs in translations.items():  # the repository's own translations come first
        have = {x["part"] for x in prs}
        pairs[k] = (prs + [x for x in pairs.get(k, []) if x["part"] not in have])[:TOP_PAIRS]
    vocab: Counter = Counter()
    for f in files.values():
        vocab.update(f.get("vocab", ()))
    lex_tmp = Lexicon(vocab=dict(vocab), n_files=len(files))
    seed_hits = {}
    for key, targets in seed_entries().items():
        kept = [t for t in targets if lex_tmp.grounded(t)]
        if kept:
            seed_hits[key] = kept
    raw = {
        "version": LEXICON_VERSION, "built_at": now(), "tree_hash": tree_hash,
        "params": {"g2_min": G2_MIN, "min_support": MIN_SUPPORT, "top_pairs": TOP_PAIRS,
                   "common_part_share": COMMON_PART_SHARE, "common_part_min_units": COMMON_PART_MIN_UNITS},
        "units": n_units, "n_files": len(files), "stop_parts": stop, "pairs": pairs,
        "seed_hits": dict(sorted(seed_hits.items())), "phrases": phrases, "vocab": dict(sorted(vocab.items())),
        "text_sites": text_sites, "files": dict(sorted(files.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(raw, ensure_ascii=False, separators=(",", ":")))  # the C encoder: same text, faster
    tmp.replace(path)
    return {"path": str(path), "files": len(files), "reparsed": reparsed, "units": n_units,
            "words": len(pairs), "pairs": sum(len(v) for v in pairs.values()),
            "translation_pairs": sum(len(v) for v in translations.values()), "phrases": len(phrases),
            "seed_grounded": len(seed_hits), "bytes": path.stat().st_size,
            "seconds": round(time.monotonic() - t0, 3)}


def show(repo: Path, word: str, graph=None) -> dict:
    """Audit view of one word: learned pairs with sites, grounded seed entries, text sites."""
    lex = load(repo) or (from_graph(graph) if graph is not None else Lexicon(source="empty"))
    folded = [tn.fold_tr(w) for w in word.split()]
    return {"word": word, "keys": word_keys(word), "lexicon": lex.summary(),
            "pairs": lex.associations(word), "seed": lex.seed(folded), "text_sites": lex.text_hits(word),
            "in_vocabulary": lex.has(word)}
