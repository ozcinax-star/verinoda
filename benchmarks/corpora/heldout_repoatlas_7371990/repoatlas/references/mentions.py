"""Reference mentions in Turkish/English text (docs/DESIGN.md D10, stdlib only, no network).

:func:`extract` returns the mentions of a user message in text order. Every
mention records the span it covers in the *original* text (never in the
``fold_tr`` output), the cleaned text (trailing punctuation and a Turkish
apostrophe suffix such as ``'in`` / ``'deki`` removed; the suffix is kept in
``stripped_suffix``), the pattern that found it (``extractor``) and how sure
the pattern is (``confidence``: ``exact`` for self-describing identifiers such
as URLs and DOIs, ``pattern`` for shapes such as ``pkg==1.2``, ``heuristic``
for cue-based guesses such as a package name before a version).

Patterns run from the most specific to the least specific and spans are
exclusive: a version inside a URL, a ``pkg==x.y`` or a purl is part of that
mention and is never reported again on its own.

Kinds (schema ``repoatlas.mention/1``):

* references - ``url``, ``swhid``, ``purl``, ``arxiv``, ``doi``, ``gomod``,
  ``maven_gav``, ``repo_slug``, ``repo_at``, ``xref_issue``, ``xref_pr``,
  ``xref_mr``, ``pkgspec``, ``file``, ``symbol``, ``app``, ``name_candidate``
* qualifiers (bound to a reference later) - ``version``, ``version_candidate``,
  ``date``, ``floating``, ``local_pin``, ``sha``

A bare ``3.11`` with no cue (``v``, three components, a version word or a name
right before it) is only a ``version_candidate``. Relative version words
(``eski sürüm``, ``previous release``) are ``version_candidate`` with
``normalized.relative = True``: they always need a question to the user.
"""

from __future__ import annotations

import calendar
import re

from repoatlas import textnorm as tn

REFERENCE_KINDS = frozenset({
    "url", "swhid", "purl", "arxiv", "doi", "gomod", "maven_gav", "repo_slug", "repo_at", "xref_issue",
    "xref_pr", "xref_mr", "pkgspec", "file", "symbol", "app", "name_candidate",
})
QUALIFIER_KINDS = frozenset({"version", "version_candidate", "date", "floating", "local_pin", "sha"})
# References that identify themselves: a bare number right after one of them is its version. After a mere
# name candidate ("requests 2.31") it stays a candidate until the resolver grounds the name.
SELF_IDENTIFYING = frozenset({"url", "swhid", "purl", "gomod", "repo_slug", "repo_at", "pkgspec", "arxiv", "doi",
                              "maven_gav"})
KINDS = REFERENCE_KINDS | QUALIFIER_KINDS

TRAIL = ".,;:!?)]}>\"'»“”’`"
_APOS = "'’ʼ"
# A Turkish case/possessive suffix written after an apostrophe (requests'in, v2.31'de, main'deki, #12'de).
_TR_SUFFIX = re.compile(r"[" + _APOS + r"][a-zçğıöşüâîû]{1,14}$", re.I)
_TR_SUFFIX_AFTER = re.compile(r"[" + _APOS + r"][a-zçğıöşüâîû]{1,14}\b", re.I)
_GAP = re.compile(r"(?:[" + _APOS + r"][a-zçğıöşüâîû]{1,14})?\s{1,3}", re.I)

_MONTHS_EN = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS_EN.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS_TR = {"ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7,
              "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12}
_MONTH_WORDS = ("January|February|March|April|May|June|July|August|September|October|November|December|"
                "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|"
                "Ocak|Şubat|Subat|Mart|Nisan|Mayıs|Mayis|Haziran|Temmuz|Ağustos|Agustos|Eylül|Eylul|Ekim|"
                "Kasım|Kasim|Aralık|Aralik")

# Known runtimes: "Python 3.11" names a runtime, not a package.
RUNTIMES = {"python": "python", "cpython": "python", "node": "node", "nodejs": "node", "node.js": "node",
            "go": "go", "golang": "go", "rust": "rust", "java": "java", "jdk": "java"}
_VERSION_WORDS = r"(?:version|versions|release|tag|v|sürüm\w*|surum\w*|versiyon\w*|sürümü\w*|etiket\w*)"
_CUE_BEFORE = re.compile(r"(?:\b(?:version|release|tag|sürüm\w*|surum\w*|versiyon\w*|etiket\w*|"
                         r"python|cpython|node|nodejs|go|golang|java|jdk|rust)\s*)$", re.I)
_CUE_AFTER = re.compile(r"^[" + _APOS + r"]?\w*\s*(?:sürüm|surum|versiyon|version|release|dok[uü]man|docs|"
                        r"belge|tag|etiket)", re.I)
_NAME_STOP = {
    "version", "versions", "release", "releases", "tag", "tags", "and", "or", "the", "in", "on",
    "at", "to", "of", "for", "with", "from", "is", "was", "ve", "ile", "veya", "bu", "şu", "su", "o", "bir",
    "since", "until", "before", "after", "than", "then", "sürüm", "sürümü", "versiyon", "chapter", "section",
    "page", "line", "lines", "step", "item", "table", "figure", "rfc", "pep", "issue", "pr", "mr", "#",
    "about", "around", "over", "under", "just", "only", "uses", "use", "using", "by", "per", "times", "x",
    "every", "each", "yaklaşık", "yaklasik", "are", "be", "a", "an", "were", "has", "have",
}

_EXT = ("py|pyi|pyx|js|mjs|cjs|jsx|ts|tsx|go|rs|java|kt|kts|rb|php|cs|c|h|cc|cpp|hpp|swift|scala|lua|md|rst|"
        "toml|json|yaml|yml|cfg|ini|lock|sh|ps1|sql|proto|gradle|txt|html")

# (kind, compiled regex, group holding the mention, extractor id, confidence)
_P: list[tuple[str, re.Pattern, int, str, str]] = []


def _p(kind: str, rx: str, grp: int = 0, *, name: str, conf: str, flags: int = 0) -> None:
    _P.append((kind, re.compile(rx, flags), grp, name, conf))


# -- identifiers that describe themselves ---------------------------------------------------------
_p("url", r"<?\bhttps?://[^\s<>\"'`()\[\]{}’“”]+", name="regex:url", conf="exact")
_p("swhid", r"\bswh:1:(?:cnt|dir|rev|rel|snp):[0-9a-f]{40}(?:;[\w.:/=%,+-]+)*", name="regex:swhid", conf="exact")
_p("purl", r"\bpkg:[a-z][a-z0-9.+-]*/[^\s@?#,;]+(?:@[^\s?#,;]+)?(?:\?[^\s#,;]*)?(?:#[^\s,;]*)?",
   name="regex:purl", conf="exact")
_p("doi", r"(?:\bdoi:\s?)?(?<![\w/.])10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", name="regex:doi", conf="exact",
   flags=re.I)
_p("arxiv", r"\barXiv:\s?(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\b", name="regex:arxiv_prefixed",
   conf="exact", flags=re.I)
# bare new-style id YYMM.NNNNN(vN): the month is checked in _accept
_p("arxiv", r"(?<![\w./-])\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}(?:v\d+)?(?![\w.])", name="regex:arxiv_bare", conf="pattern")
_p("gomod", r"(?<![\w./@-])(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[\w.~-]+)+@v\d+\.\d+\.\d+[\w.+-]*",
   name="regex:gomod", conf="exact")
_p("maven_gav", r"(?<![\w.:/-])[a-z][\w-]*(?:\.[\w-]+)+:[A-Za-z][\w.-]*:\d[\w.-]*", name="regex:maven_gav",
   conf="pattern")
# -- repository and package shapes -----------------------------------------------------------------
_p("repo_at", r"(?<![\w./@-])[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}@"
              r"(?:[0-9a-f]{7,40}\b|[\w.+-]+(?:/[\w.+-]+)*)",
   name="regex:repo_at", conf="pattern")
_p("xref_mr", r"(?<![\w./@-])[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}!\d{1,7}\b",
   name="regex:repo_mr", conf="pattern")
_p("xref_issue", r"(?<![\w./@-])[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}#\d{1,7}\b",
   name="regex:repo_xref", conf="pattern")
_p("pkgspec", r"(?<![\w./@-])(?:@[a-z0-9][\w.-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[\w,.-]*\])?\s?"
              r"(?:===|==|~=|!=|>=|<=)\s?v?\d+(?:\.\d+){0,3}[\w.+*-]*(?:\s?,\s?(?:==|!=|>=|<=|<|>|~=)\s?\d[\w.*+-]*)*",
   name="regex:pip_spec", conf="pattern")
_p("pkgspec", r"(?<![\w./-])(?:@[a-z0-9][\w.-]*/)?[a-z0-9][a-z0-9._-]*@(?:\^|~|>=|<=|=)?v?\d+(?:\.\d+){0,2}[\w.+-]*",
   name="regex:at_spec", conf="pattern")
_p("xref_pr", r"\b(?:PR|pull request|pull req|çekme isteği|cekme istegi)\s?#?\d{1,7}\b", name="regex:pr_word",
   conf="pattern", flags=re.I)
_p("xref_mr", r"\b(?:MR|merge request)\s?!?\d{1,7}\b", name="regex:mr_word", conf="pattern", flags=re.I)
_p("xref_issue", r"\b(?:issue|sorun|bug)\s?#\d{1,7}\b|\bGH-\d{1,7}\b", name="regex:issue_word", conf="pattern",
   flags=re.I)
_p("xref_issue", r"(?<![\w&/#])#\d{1,7}\b", name="regex:hash_number", conf="pattern")
_p("xref_mr", r"(?<![\w!])!\d{1,7}\b", name="regex:bang_number", conf="pattern")
# -- backticked code -----------------------------------------------------------------------------
_p("file", r"`((?:[\w.-]+/)+[\w.-]+|[\w-]+(?:\.[\w-]+)*\.(?:" + _EXT + r"))`", 1, name="regex:backtick_path",
   conf="pattern")
_p("symbol", r"`([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*(?:\(\))?)`", 1, name="regex:backtick_symbol", conf="pattern")
# -- local intent, floating intent, apps, dates ------------------------------------------------------
_p("local_pin", r"\b(?:the version we (?:use|run|have|ship)|versions? we use|we(?:'re| are)? using|we use|"
                r"our (?:version|lock ?file|locked version|installed version|pinned version)|locally installed|"
                r"installed version|locked version|version in our (?:project|lock ?file)|"
                r"bizim kullandığımız|bizim kullandigimiz|kullandığımız|kullandigimiz|kullanıyoruz|kullaniyoruz|"
                r"kullandığım|kullandigim|kullanıyorum|kullaniyorum|projedeki sürüm\w*|projedeki surum\w*|"
                r"bizdeki sürüm\w*|bizdeki surum\w*|kurulu sürüm\w*|kurulu surum\w*|yüklü sürüm\w*|"
                r"yuklu surum\w*|kilitli sürüm\w*|kilitli surum\w*)",
   name="regex:local_pin", conf="pattern", flags=re.I)
_p("floating", r"\b(?:latest|newest|most recent|en son|en yeni|güncel|guncel|son sürüm\w*|son surum\w*|"
               r"current (?:version|release|master|main)|tip of (?:main|master|trunk)|"
               r"(?:on|in|from|at) (?:main|master|trunk|HEAD)\b|(?:main|master|default) branch|"
               r"(?:main|master) dal\w*|varsayılan dal\w*|varsayilan dal\w*|HEAD)\b",
   name="regex:floating", conf="pattern")
_p("floating", r"\b(?:main|master|trunk)[" + _APOS + r"](?:de|da|deki|daki|den|dan|e|a|ye|ya|i|ı)\b",
   name="regex:floating_tr_suffix", conf="pattern", flags=re.I)
_p("version_candidate", r"\b(?:eski|önceki|onceki|geçen|gecen|ilk|old|older|previous|prior|legacy|original|former)"
                        r"\s+(?:\w+\s+)?(?:sürüm\w*|surum\w*|versiyon\w*|version\w*|release\w*|tag\w*|commit\w*|"
                        r"dal\w*|branch\w*|hali\w*)",
   name="regex:relative_version", conf="heuristic", flags=re.I)
_p("app", r"\b(?i:like|as|the way|similar to)\s+([A-Z][\w.+-]*(?:\s+[A-Z][\w.+-]*){0,2})\s+(?:does|do|did|handles?)\b",
   1, name="regex:app_en", conf="heuristic")
_p("app", r"\b([A-Z][\w.+-]*(?:\s+[A-Z][\w.+-]*){0,2})[" + _APOS + r"](?:un|ün|in|ın|nin|nın|nun|nün)\s+"
          r"(?:yaptığı|yaptigi|yaptığını|yaptigini|kullandığı|kullandigi|yaptığı gibi|yaptigi gibi)",
   1, name="regex:app_tr", conf="heuristic")
_p("date", r"\b\d{4}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?\b", name="regex:iso_date", conf="exact")
_p("date", r"\b(?:(?:0?[1-9]|[12]\d|3[01])\s+)?(?:" + _MONTH_WORDS + r")\.?\s+(?:(?:0?[1-9]|[12]\d|3[01]),\s+)?"
           r"(?:19|20)\d{2}\b", name="regex:month_year", conf="pattern", flags=re.I)
# -- commits, paths, symbols ------------------------------------------------------------------------------
_p("sha", r"(?<![\w/.:@#-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?![\w-])", name="regex:sha", conf="pattern")
_p("file", r"(?<![\w/.:@-])(?:[\w.-]+/)*[\w-]+\.(?:" + _EXT + r")(?![\w/])", name="regex:path", conf="pattern")
_p("symbol", r"(?<![\w.`])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\(\)", name="regex:call_symbol", conf="pattern")
# -- names next to versions, Turkish genitive names, cue words ---------------------------------------------
_p("name_candidate", r"(?<![\w/.@-])([A-Za-z][\w.+-]{1,40}?)(?:[" + _APOS + r"][a-zçğıöşü]{1,8})?\s+(?=v?\d+\.\d)",
   1, name="regex:name_before_version", conf="heuristic")
_p("name_candidate", r"(?<![\w/.@-])([a-z][a-z0-9_.-]{1,40})[" + _APOS + r"](?:in|ın|un|ün|nin|nın|nun|nün|"
                     r"de|da|te|ta|deki|daki|teki|taki|den|dan|ten|tan|e|a|ye|ya|i|ı|u|ü|yi|yı|yu|yü|le|la)\b",
   1, name="regex:tr_case_name", conf="heuristic")
_p("name_candidate", r"\b(?:package|library|lib|crate|module|gem|kütüphane(?:si)?|kutuphane(?:si)?|paket(?:i)?)\s+"
                     r"([A-Za-z][\w.-]{1,40})\b", 1, name="regex:cue_name", conf="heuristic", flags=re.I)
_p("name_candidate", r"\b([A-Za-z][\w.-]{1,40})\s+(?:package|library|crate|module|kütüphanesi|kutuphanesi|paketi)\b",
   1, name="regex:name_cue", conf="heuristic", flags=re.I)
_p("repo_slug", r"(?:\b(?:repo|repository|repos|depo|deposu|github|gitlab|fork)\s+)"
                r"([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})\b", 1, name="regex:cued_slug",
   conf="pattern", flags=re.I)
# -- versions last ----------------------------------------------------------------------------------------------
_p("version", r"(?<![\w.])v?\d+\.\d+(?:\.\d+){0,2}(?:[-.]?(?:a|b|rc|dev|post|alpha|beta)\.?\d*)?(?:\.x)?(?![\w.])"
              r"|(?<![\w.])v\d+(?![\w.])", name="regex:version", conf="pattern", flags=re.I)


def clean(token: str, start: int) -> tuple[str, int, str | None]:
    """Strip trailing punctuation and one Turkish apostrophe suffix -> (text, end, suffix)."""
    end = start + len(token)
    suffix = None
    changed = True
    while changed and token:
        changed = False
        while token and token[-1] in TRAIL:
            if token[-1] == ")" and token.count("(") > token.count(")") - 1 and "(" in token:
                break  # keep a closing parenthesis that belongs to the token, e.g. foo()
            token, end = token[:-1], end - 1
            changed = True
        m = _TR_SUFFIX.search(token)
        if m and suffix is None:
            suffix = token[m.start():]
            token, end = token[:m.start()], start + m.start()
            changed = True
    if token.startswith("<"):
        token, start = token[1:], start + 1
    return token, end, suffix


def _cue_version(text: str, s: int, e: int, tok: str) -> bool:
    if tok.lower().startswith("v") or tok.count(".") >= 2:
        return True
    before = tn.fold_tr(text[max(0, s - 40):s]).rstrip()
    if _CUE_BEFORE.search(before + " ") or _CUE_BEFORE.search(before):
        return True
    return bool(_CUE_AFTER.search(text[e:e + 30]))


def _accept(kind: str, name: str, tok: str) -> bool:
    if kind == "arxiv" and name == "regex:arxiv_bare":
        yy = int(tok[:2])
        return 7 <= yy <= 99
    if kind == "name_candidate":
        low = tn.fold_tr(tok).strip(".-")
        if low in _NAME_STOP or tn.fold_tr(low) in tn.TR_STOPWORDS or low in tn.EN_STOPWORDS:
            return False
        if re.fullmatch(r"v?\d[\w.]*", low):
            return False
    if kind == "sha":
        return not re.fullmatch(r"\d+", tok)
    return True


def _normalize(kind: str, tok: str) -> dict:
    """Kind-specific structure of a mention (no lookups)."""
    if kind == "arxiv":
        m = re.search(r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", tok, re.I)
        return {"arxiv_id": m.group(1), "version": m.group(2)} if m else {}
    if kind == "doi":
        return {"doi": re.sub(r"^(?:doi:\s?)", "", tok, flags=re.I)}
    if kind in ("xref_issue", "xref_pr", "xref_mr"):
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)[#!](\d+)$", tok)
        if m:
            return {"owner": m.group(1), "repo": m.group(2), "number": int(m.group(3))}
        return {"number": int(re.sub(r"\D", "", tok))}
    if kind == "repo_at":
        slug, _, ref = tok.partition("@")
        owner, _, repo = slug.partition("/")
        return {"owner": owner, "repo": repo, "ref": ref}
    if kind == "repo_slug":
        owner, _, repo = tok.partition("/")
        return {"owner": owner, "repo": repo}
    if kind == "version":
        return {"version": tok}
    if kind == "sha":
        return {"sha": tok.lower()}
    if kind == "date":
        return _parse_date(tok)
    if kind == "floating":
        low = tn.fold_tr(tok)
        branch = next((b for b in ("main", "master", "trunk") if re.search(rf"\b{b}\b", low)), None)
        return {"branch": branch} if branch else {}
    if kind == "name_candidate":
        low = tok.lower()
        return {"name": tok, **({"runtime": RUNTIMES[low]} if low in RUNTIMES else {})}
    return {}


def _parse_date(tok: str) -> dict:
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", tok)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        words = re.findall(r"[^\W\d_]+", tn.fold_tr(tok))
        nums = [int(x) for x in re.findall(r"\d+", tok)]
        months = [_MONTHS_EN.get(w) or _MONTHS_TR.get(w) for w in words]
        mo = next((m for m in months if m), 1)
        y = next(n for n in nums if n >= 1900)
        small = [n for n in nums if n < 1900]
        d = str(small[0]) if small else None
    last = calendar.monthrange(y, mo)[1]
    if d:
        day = f"{y:04d}-{mo:02d}-{int(d):02d}"
        return {"date_from": day, "date_to": day, "relative": False}
    return {"date_from": f"{y:04d}-{mo:02d}-01", "date_to": f"{y:04d}-{mo:02d}-{last:02d}", "relative": False}


def extract(text: str) -> list[dict]:
    """Mentions of ``text`` sorted by position, with ids ``m1``, ``m2``, ... (see the module docstring)."""
    text = text or ""
    out: list[dict] = []
    taken: list[tuple[int, int]] = []
    for kind, rx, grp, name, conf in _P:
        for m in rx.finditer(text):
            s = m.start(grp)
            raw = m.group(grp)
            tok, e, suffix = clean(raw, s)
            if tok and raw.startswith("<") and grp == 0:
                s += 1
            if not tok or any(a < e and s < b for a, b in taken):
                continue
            if not _accept(kind, name, tok):
                continue
            k = kind
            norm = _normalize(kind, tok)
            if kind == "version" and not _cue_version(text, s, e, tok):
                k = "version_candidate"
            if kind == "version_candidate" and name == "regex:relative_version":
                norm = {"relative": True, "phrase": tok}
            if kind == "xref_pr" and name == "regex:mr_word":
                k = "xref_mr"
            if suffix is None:
                follow = _TR_SUFFIX_AFTER.match(text, e)
                suffix = follow.group(0) if follow else None
            taken.append((s, e))
            out.append({"kind": k, "span": [s, e], "text": tok, "stripped_suffix": suffix,
                        "normalized": norm, "extractor": name, "confidence": conf,
                        "binds_to": None, "binding": None})
    out.sort(key=lambda x: x["span"][0])
    for prev, cur in zip(out, out[1:]):
        # "requests 2.31", "django'nun 4.2 sürümü": a name or reference right before a bare number is its cue
        gap = text[prev["span"][1]:cur["span"][0]]
        if cur["kind"] == "version_candidate" and not cur["normalized"].get("relative") \
                and prev["kind"] in SELF_IDENTIFYING and _GAP.fullmatch(gap):
            cur["kind"] = "version"
    for i, m in enumerate(out, 1):
        m["id"] = f"m{i}"
    return out


def clauses(text: str, mentions: list[dict]) -> list[tuple[int, int]]:
    """Sentence-level segments ``[(start, end)]``: split at ``. ! ? ;`` + space and newlines outside mentions."""
    inside = [(m["span"][0], m["span"][1]) for m in mentions]
    cuts = [0]
    for mm in re.finditer(r"[.!?;](?=\s|$)|\n", text):
        p = mm.end()
        if any(a <= mm.start() < b for a, b in inside):
            continue
        cuts.append(p)
    cuts.append(len(text))
    segs = []
    for a, b in zip(cuts, cuts[1:]):
        if b > a:
            segs.append((a, b))
    return segs or [(0, len(text))]


def clause_of(segments: list[tuple[int, int]], pos: int) -> int:
    for i, (a, b) in enumerate(segments):
        if a <= pos < b:
            return i
    return len(segments) - 1
