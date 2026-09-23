"""Benchmark metrics: token counting, locators, gold-fact matching, negative facts.

Every rule here is deliberately simple and mechanical so a reader can re-check
a score by hand (the rules are restated in docs/BENCHMARKS.md):

* **Locator** - ``path:N``, ``path:N-M``, ``path:LN`` or Graphify's
  ``src=path loc=LN``; retrieval JSON ``"file": path, "lines": [a, b]``.
  A locator is a closed line range on one repo-relative path.
* **Fact found** - a gold fact counts as found when *any* of its
  ``match_any`` alternatives matches the delivered text:
  ``{"loc": "path:a-b"}``  some locator in the text is on ``path`` and its range
  overlaps ``a-b``; ``{"text": "tok"}`` the literal token occurs (case-sensitive,
  word-bounded at alphanumeric ends); ``{"all": [...]}`` every token occurs.
* **Pinpointed** - found through a ``loc`` alternative whose matching locator
  spans at most :data:`PINPOINT_MAX_LINES` lines (a pointer, not a whole file).
* **Negative fact** - a known-wrong statement. It is matched only against
  *assertions* (Graphify ``EDGE`` lines, Verinoda claim texts, retrieval
  edges, the ``calls:`` / ``called by:`` outlines of retrieval text), never
  against source text, because quoting code asserts nothing.
* **Facts per 1k tokens** - gold facts found / delivered tokens x 1000, summed
  over a set's questions (:func:`per_1k`).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

PINPOINT_MAX_LINES = 30

# -- token counting --------------------------------------------------------------

_TOKENIZER: dict = {}


def token_method() -> str:
    """'tiktoken cl100k_base' when usable, else 'chars/4 estimate' (decided once)."""
    if "method" not in _TOKENIZER:
        try:
            import tiktoken  # type: ignore[import-not-found]

            enc = tiktoken.get_encoding("cl100k_base")
            enc.encode("probe")
            _TOKENIZER.update(method="tiktoken cl100k_base", enc=enc)
        except Exception:  # not installed, or its BPE file cannot be fetched offline
            _TOKENIZER.update(method="chars/4 estimate", enc=None)
    return _TOKENIZER["method"]


def count_tokens(text: str) -> int:
    """Token count of ``text`` with the method reported by :func:`token_method`."""
    token_method()
    enc = _TOKENIZER.get("enc")
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    return math.ceil(len(text) / 4)


def reset_tokenizer() -> None:
    """Forget the cached tokenizer decision (tests)."""
    _TOKENIZER.clear()


def per_1k(count: int, tokens: int) -> float | None:
    """``count`` per 1,000 delivered tokens (e.g. gold facts found per 1k tokens), 2 decimals."""
    return round(1000 * count / tokens, 2) if tokens else None


# -- locators --------------------------------------------------------------------

@dataclass(frozen=True)
class Locator:
    path: str
    start: int
    end: int

    @property
    def span(self) -> int:
        return self.end - self.start + 1

    def overlaps(self, other: "Locator") -> bool:
        return self.path == other.path and self.start <= other.end and other.start <= self.end

    def __str__(self) -> str:
        return f"{self.path}:{self.start}" + (f"-{self.end}" if self.end != self.start else "")


_PATH = r"[A-Za-z0-9_.][\w.\-/\\]*\.[A-Za-z0-9_]+"
_LOC_RES = [
    # Graphify NODE lines: [src=orders/repository.py loc=L15 ...]
    re.compile(rf"src=(?P<path>{_PATH}) loc=L(?P<a>\d+)"),
    # retrieval JSON: "file": "path", "lines": [a, b]
    re.compile(rf'"file":\s*"(?P<path>{_PATH})",\s*"lines":\s*\[(?P<a>\d+),\s*(?P<b>\d+)\]'),
    # path:15  path:15-20  path:L15  path:L15-L20. The path must start a token
    # (so absolute "C:/x/y.py:3" never yields a bogus relative "x/y.py").
    re.compile(rf"(?<![\w.\-/\\])(?P<path>{_PATH}):L?(?P<a>\d+)(?:-L?(?P<b>\d+))?(?!\d)"),
]


def norm_path(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def parse_locator(spec: str) -> Locator:
    """``'path:a'`` or ``'path:a-b'`` -> :class:`Locator`."""
    path, _, rng = spec.rpartition(":")
    if not path:
        raise ValueError(f"not a locator: {spec!r}")
    a, _, b = rng.lstrip("L").partition("-")
    start, end = int(a), int(b.lstrip("L") or a)
    return Locator(norm_path(path), min(start, end), max(start, end))


def extract_locators(text: str) -> list[Locator]:
    """All file:line locators in ``text`` (deduplicated, in order of appearance)."""
    found: dict[Locator, int] = {}
    for rx in _LOC_RES:
        for m in rx.finditer(text or ""):
            a = int(m.group("a"))
            b = int(m.groupdict().get("b") or a)
            if a < 1:
                continue
            loc = Locator(norm_path(m.group("path")), min(a, b), max(a, b))
            found.setdefault(loc, m.start())
    return sorted(found, key=lambda l: found[l])


# -- token matching ----------------------------------------------------------------

def token_regex(tok: str) -> re.Pattern:
    """Literal, case-sensitive; word-bounded where the token starts/ends alphanumerically."""
    pre = r"(?<!\w)" if tok[:1].isalnum() or tok[:1] == "_" else ""
    post = r"(?!\w)" if tok[-1:].isalnum() or tok[-1:] == "_" else ""
    return re.compile(pre + re.escape(tok) + post)


def has_token(text: str, tok: str) -> bool:
    return bool(tok) and token_regex(tok).search(text or "") is not None


# -- gold facts ----------------------------------------------------------------------

def match_fact(fact: dict, text: str, locators: list[Locator] | None = None) -> dict:
    """Does ``text`` contain the fact? Returns {"found", "pinpointed", "via"}."""
    locs = extract_locators(text) if locators is None else locators
    best: dict = {"found": False, "pinpointed": False, "via": None}
    for alt in fact.get("match_any") or []:
        if "loc" in alt:
            want = parse_locator(alt["loc"])
            hits = [l for l in locs if l.overlaps(want)]
            if hits:
                tight = min(hits, key=lambda l: l.span)
                pin = tight.span <= PINPOINT_MAX_LINES
                if not best["found"] or (pin and not best["pinpointed"]):
                    best = {"found": True, "pinpointed": pin, "via": f"loc {tight}"}
        elif "text" in alt:
            if not best["found"] and has_token(text, alt["text"]):
                best = {"found": True, "pinpointed": False, "via": f"text {alt['text']!r}"}
        elif "all" in alt:
            if not best["found"] and all(has_token(text, t) for t in alt["all"]):
                best = {"found": True, "pinpointed": False, "via": "all " + ", ".join(map(repr, alt["all"]))}
        else:
            raise ValueError(f"fact {fact.get('id')}: unknown match alternative {alt}")
    return best


def score_facts(facts: list[dict], text: str) -> dict:
    locs = extract_locators(text)
    per = {f["id"]: match_fact(f, text, locs) for f in facts}
    found = [k for k, v in per.items() if v["found"]]
    return {
        "total": len(facts), "found_n": len(found),
        "pinpointed_n": sum(1 for v in per.values() if v["pinpointed"]),
        "found": found, "missed": [k for k in per if k not in found],
        "via": {k: v["via"] for k, v in per.items() if v["found"]},
    }


def validate_gold(root: Path, questions: list[dict], corpus_files: list[str] | None = None) -> dict:
    """Re-check every gold fact against the corpus before any approach is scored.

    * ``source.at`` must exist and its lines must contain ``source.contains``;
    * every ``loc`` alternative must point at existing lines;
    * every ``text``/``all`` token must occur in some corpus file.
    """
    root = Path(root)
    cache: dict[str, list[str] | None] = {}

    def lines_of(rel: str) -> list[str] | None:
        if rel not in cache:
            try:
                cache[rel] = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                cache[rel] = None
        return cache[rel]

    corpus_text = None
    failures: list[str] = []
    checked = 0
    for q in questions:
        for f in q.get("facts", []):
            checked += 1
            src = f.get("source") or {}
            if not src.get("at"):
                failures.append(f"{f['id']}: no source.at")
                continue
            loc = parse_locator(src["at"])
            ls = lines_of(loc.path)
            if ls is None:
                failures.append(f"{f['id']}: {loc.path} does not exist")
                continue
            if loc.end > len(ls):
                failures.append(f"{f['id']}: {loc} beyond end of file ({len(ls)} lines)")
                continue
            block = "\n".join(ls[loc.start - 1 : loc.end])
            if src.get("contains") and src["contains"] not in block:
                failures.append(f"{f['id']}: {loc} does not contain {src['contains']!r}: {block.strip()[:120]!r}")
            for alt in f.get("match_any") or []:
                if "loc" in alt:
                    al = parse_locator(alt["loc"])
                    als = lines_of(al.path)
                    if als is None or al.end > len(als):
                        failures.append(f"{f['id']}: match locator {al} does not exist")
                else:
                    toks = [alt["text"]] if "text" in alt else list(alt.get("all", []))
                    if corpus_text is None:
                        if corpus_files is None:
                            from verinoda.snapshot import list_files

                            corpus_files = list_files(root)
                        corpus_text = "\n".join("\n".join(lines_of(p) or []) for p in corpus_files)
                    for t in toks:
                        if not has_token(corpus_text, t):
                            failures.append(f"{f['id']}: token {t!r} does not occur in the corpus")
    return {"ok": not failures, "facts_checked": checked, "failures": failures}


# -- assertions & negative facts -------------------------------------------------------

_EDGE_RE = re.compile(r"^EDGE (?P<src>.+?) --(?P<rel>[\w-]+) \[(?P<conf>[^\]]*)\]--> (?P<tgt>.+?)(?: at=(?P<at>\S+))?\s*$")
_CLAIM_REL_RE = re.compile(r"^`(?P<src>[^`]+)` (?P<rel>[a-z_]+) `(?P<tgt>[^`]+)`(?: \((?P<at>[^)]+)\))?")
_CHAIN_RE = re.compile(r"^Data path (?P<chain>.+?) reaches persistence")


def norm_label(label: str) -> str:
    """`OrderRepository.save()` / `.save()` / 'save' -> 'save'; 'service.py' stays."""
    s = (label or "").strip().strip("`'\"").strip()
    s = s.split("(")[0].strip()
    if re.search(r"\.(py|js|ts|tsx|go|rs|java|rb|md|json|toml|ya?ml)$", s, re.I):
        return s.lower().rsplit("/", 1)[-1]
    s = s.lstrip(".")
    if "." in s:
        s = s.rpartition(".")[2]
    return s.lower()


def graphify_assertions(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        m = _EDGE_RE.match(line.strip())
        if m:
            at = m.group("at")
            out.append({"src": m.group("src"), "rel": m.group("rel"), "tgt": m.group("tgt"),
                        "confidence": m.group("conf").split()[0] if m.group("conf") else None,
                        "at": _norm_at(at), "text": line.strip()})
    return out


def claim_assertions(claims: list[dict]) -> list[dict]:
    """Relation triples and data-path chains stated by Verinoda claim texts."""
    out = []
    for c in claims:
        t = c.get("text", "")
        m = _CLAIM_REL_RE.match(t)
        if m:
            out.append({"src": m.group("src"), "rel": m.group("rel"), "tgt": m.group("tgt"),
                        "at": _norm_at(m.group("at")), "text": t, "status": c.get("status"), "claim": c.get("id")})
            continue
        m = _CHAIN_RE.match(t)
        if m:
            hops = [h.strip() for h in m.group("chain").split(" -> ")]
            for a, b in zip(hops, hops[1:]):
                out.append({"src": a, "rel": "calls", "tgt": b, "at": None, "text": t,
                            "status": c.get("status"), "claim": c.get("id"), "chain": True})
            continue
        out.append({"src": None, "rel": None, "tgt": None, "at": None, "text": t,
                    "status": c.get("status"), "claim": c.get("id")})
    return out


def retrieval_assertions(edges: list[dict]) -> list[dict]:
    return [{"src": e.get("from"), "rel": e.get("relation"), "tgt": e.get("to"),
             "confidence": e.get("confidence"), "at": _norm_at(e.get("at")),
             "text": f"{e.get('from')} -{e.get('relation')}-> {e.get('to')} @{e.get('at')}"} for e in edges]


_TEXT_HEAD_RE = re.compile(r"^## (?P<path>\S+?):(?P<a>\d+)-(?P<b>\d+)(?: (?P<sig>.*))?$")
_SIG_NAME_RE = re.compile(r"\b(?:async\s+def|def|class|function|func|fn|interface|struct|enum|trait|type)\s+"
                          r"(?P<name>[A-Za-z_$][\w$]*)")
_FIRST_NAME_RE = re.compile(r"^\s*(?P<name>[A-Za-z_$][\w$.]*)")
_MORE_RE = re.compile(r"\s*\(\+\d+\)$")
_CALLEE_RE = re.compile(r"^(?P<name>.+?)(?:@(?P<ln>\d+))?(?P<inf>\?)?$")
_CALLER_RE = re.compile(r"^(?P<name>.+?) \((?P<loc>[^()]*)\)(?P<inf>\?)?$")
_OUTLINE_PREFIXES = ("  doc: ", "  calls: ", "  called by: ", "  const ")


def _sig_name(sig: str) -> str | None:
    m = _SIG_NAME_RE.search(sig or "") or _FIRST_NAME_RE.match(sig or "")
    return m.group("name") if m else None


def _caller(entry: str) -> tuple[str, str | None, str] | None:
    m = _CALLER_RE.match(_MORE_RE.sub("", entry.strip()))
    if not m:
        return None
    loc = m.group("loc")
    at = loc if re.search(r":\d+$", loc) else None
    return m.group("name").strip(), at, "INFERRED" if m.group("inf") else "EXTRACTED"


def text_outline_assertions(text: str) -> list[dict]:
    """Relations stated by ``retrieval.render_text`` output: its ``calls:`` / ``called by:`` outlines.

    An item header ``## path:a-b <signature>`` is followed by
    ``  calls: name@line, ...`` (call sites in that item's file) and
    ``  called by: caller (path:line) <- caller's caller (path:line), ...; ...``.
    A trailing ``?`` marks an inferred edge. Each outline entry becomes one
    ``calls`` assertion, in the same shape and text as :func:`retrieval_assertions`.
    """
    out: list[dict] = []
    seen: set[str] = set()
    item: tuple[str, str] | None = None  # (path, name) while inside a header's outline block

    def add(src: str, tgt: str, at: str | None, conf: str) -> None:
        text = f"{src} -calls-> {tgt} @{at}"
        if text in seen:  # the same edge shown under two items is one assertion
            return
        seen.add(text)
        out.append({"src": src, "rel": "calls", "tgt": tgt, "confidence": conf, "at": _norm_at(at), "text": text})

    for line in (text or "").splitlines():
        h = _TEXT_HEAD_RE.match(line)
        if h:
            name = _sig_name(h.group("sig") or "")
            item = (h.group("path"), name) if name else None
            continue
        # The outline block is the header's own lines (doc / calls / called by / const);
        # the source excerpt that follows is quoted code and asserts nothing.
        if item is None or not line.startswith(_OUTLINE_PREFIXES):
            item = None
            continue
        body = line.strip()
        if body.startswith("calls: "):
            for entry in _MORE_RE.sub("", body[len("calls: "):]).split(", "):
                m = _CALLEE_RE.match(entry.strip())
                if not m or not m.group("name"):
                    continue
                at = f"{item[0]}:{m.group('ln')}" if m.group("ln") else None
                add(item[1], m.group("name").strip(), at, "INFERRED" if m.group("inf") else "EXTRACTED")
        elif body.startswith("called by: "):
            for entry in _MORE_RE.sub("", body[len("called by: "):]).split("; "):
                head, _, subs = entry.partition(" <- ")
                c = _caller(head)
                if c is None:
                    continue
                add(c[0], item[1], c[1], c[2])
                for sub in _MORE_RE.sub("", subs).split(", ") if subs else []:
                    s2 = _caller(sub)
                    if s2 is not None:
                        add(s2[0], c[0], s2[1], s2[2])
    return out


def _norm_at(at: str | None) -> str | None:
    if not at:
        return None
    path, _, ln = at.rpartition(":")
    ln = ln.lstrip("L")
    return f"{norm_path(path)}:{ln}" if path and ln.isdigit() else None


def _alts(spec: str) -> set[str]:
    return {norm_label(x) for x in spec.split("|") if x.strip()}


def match_negative(neg: dict, assertions: list[dict]) -> list[dict]:
    """Assertions that state the known-wrong statement ``neg``."""
    hits = []
    if neg.get("relation"):
        s, r, t = neg["relation"]
        srcs, rels, tgts = _alts(s), {x.strip().lower() for x in r.split("|")}, _alts(t)
        for a in assertions:
            if a.get("src") is None:
                continue
            if norm_label(a["src"]) in srcs and (a["rel"] or "").lower() in rels and norm_label(a["tgt"]) in tgts:
                hits.append(a)
    if neg.get("assertion_regex"):
        rx = re.compile(neg["assertion_regex"])
        hits += [a for a in assertions if rx.search(a["text"]) and a not in hits]
    return hits


# -- mechanical citation check ----------------------------------------------------------

CHECKED_RELATIONS = {"calls", "uses"}


def import_aliases(text: str) -> dict[str, set[str]]:
    """Python ``import x as y`` / ``from m import x as y`` anywhere in a file: name -> aliases (lowercase)."""
    import ast

    out: dict[str, set[str]] = {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.asname:
                    out.setdefault(a.name.rpartition(".")[2].lower(), set()).add(a.asname.lower())
    return out


def check_citations(root: Path, assertions: list[dict]) -> dict:
    """Heuristic: does the cited line of a calls/uses assertion name its target?

    The target counts as named when its last name component, or a Python
    import alias of it bound in the same file (``import x as _x``), occurs as a
    word on the cited line. Applied identically to every approach that states
    relations with a line. A failure means the location given does not show
    the relation; it does not prove the relation false.
    """
    checked, via_alias, failed = 0, 0, []
    cache: dict[str, tuple[list[str] | None, dict[str, set[str]]]] = {}
    for a in assertions:
        if (a.get("rel") or "").lower() not in CHECKED_RELATIONS or not a.get("at") or not a.get("tgt"):
            continue
        path, _, ln = a["at"].rpartition(":")
        if path not in cache:
            try:
                text = (Path(root) / path).read_text(encoding="utf-8", errors="replace")
                cache[path] = (text.splitlines(), import_aliases(text) if path.endswith(".py") else {})
            except OSError:
                cache[path] = (None, {})
        lines, aliases = cache[path]
        checked += 1
        tok = norm_label(a["tgt"])
        line = lines[int(ln) - 1] if lines and 0 < int(ln) <= len(lines) else None

        def names(t: str) -> bool:
            return line is not None and re.search(rf"\b{re.escape(t)}\b", line, re.I) is not None

        if names(tok):
            continue
        if any(names(al) for al in aliases.get(tok, ())):
            via_alias += 1
            continue
        failed.append({"assertion": a["text"][:200], "at": a["at"], "line": (line or "<missing>").strip()[:120]})
    return {"checked": checked, "cited_line_missing_target": len(failed), "named_via_import_alias": via_alias,
            "examples": failed[:5]}
