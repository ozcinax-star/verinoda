"""Honest verdicts (docs/DESIGN.md D39): do the claims that answer a sub-question answer
what it asks?

:func:`verinoda.analysis.judge` says whether claims of the right kind and strength exist. The checks here
say whether they are about the question. They only lower a verdict or refuse one, never raise it, and
they never drop a claim: a claim set aside here stays in the answer; it just cannot make the sub-question
``met`` (``flags["weak"]``), and a sub-question with an open gap is at most ``met_with_inference``
(``flags["capped"]``). Both flags are stored with the analysis, so ``verinoda plan audit`` judges the same.

- relevance: a definition ("X is defined at ...") answers only a question about where something is
  defined. A question that asks which code uses X needs claims about code that uses X; one that asks
  where X is ticked (drawn, painted, mounted: a lifecycle callback) needs a member of X named for it. Outside
  locate/define questions a definition never makes a sub-question met. A callers answer must be about
  the callee the question names, not a symbol ranked near it. A configuration read must carry one of
  the question's own words (in the variable, the reading function or its file).
- copies: claims whose evidence lies only in a reference tree (``index.reference``), a detected copy of
  the project (:mod:`verinoda.copies`) or a vendored folder never make a sub-question met, unless the
  question names that tree.
- unresolved call sites: for a callers question the indexed code files are searched for the callee's
  name written as a call, a method reference or a callback argument; sites the graph did not tie to any
  definition of that name are counted and named (an unknown), and the verdict is at most
  met_with_inference.
- why: a commit line says when code changed, not why. Without a decision record that explains it (or a
  commit subject that states a reason) the verdict is at most met_with_inference and an unknown says the
  reason was not found.
- set difference ("which X are not listed in Y", Turkish "listelenmemiş", "olmayan"): refused
  (``not_supported``) in :func:`verinoda.analysis._exclusive_guard`, next to D31's "only" questions;
  :func:`asks_set_difference` is the test.
"""

from __future__ import annotations

import re
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from verinoda import textnorm as tn

WEAK = "weak"          # flags key: claim ids that cannot make the sub-question met
CAPPED = "capped"      # flags key: reasons the sub-question is at most met_with_inference
WEAK_COPY = "weak_copy"  # flags key: the weak claims that are about what was asked, but only in a copy
SCAN_MAX_FILES = 8000          # the call-site search reads at most this many code files ...
SCAN_MAX_TOTAL = 96_000_000    # ... and this many bytes, then stops and says so
SCAN_MAX_BYTES = 2_000_000     # a larger file (generated, minified) is not read
SCAN_SECONDS = 30.0            # a last resort on a very slow disk
SITES_SHOWN = 4
VENDORED_DIRS = frozenset({"vendor", "vendored", "third_party", "third-party", "node_modules", "site-packages",
                           "bower_components"})
CODE_SUFFIXES = frozenset({".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".go",
                           ".rs", ".java", ".kt", ".kts", ".scala", ".groovy", ".rb", ".php", ".cs", ".c", ".h",
                           ".cc", ".cpp", ".hpp", ".swift", ".lua", ".dart", ".m", ".ex", ".exs", ".jl", ".zig"})
DEFINE_INTENTS = frozenset({"locate", "define"})
LOCATION_ANSWERS = DEFINE_INTENTS | {"config"}   # intents a definition or a quoted line can answer

# -- the question's shape ---------------------------------------------------------------------------------

_USAGE_EN = re.compile(
    r"\b(?:who|what|which(?:\s+[\w'-]+){0,4})\s+(?:uses?|using|calls?|invokes?|imports?|consumes?|renders?|"
    r"references?|depends? on|relies on)\b"
    r"|\bwhere\s+(?:is|are|was|were)\b.+\b(?:used|called|invoked|imported|referenced|consumed|rendered)\b"
    r"|\bused by\b|\bcallers?\b|\busages?\s+of\b|\bcall\s*sites?\b")
_USAGE_TR = re.compile(r"\bkullan(?:an|iyor|ilan|ildig|ilir|ir)\w*|\bcagir(?:an|iyor|ir)\w*|\bcagril\w*")
# "where is X ticked?": the lifecycle callbacks a framework or a game loop runs on an object. The answer is a
# member of X made for it (Wisp.tick, EmberForgeBlockEntity.serverTick); code of other classes that has the
# word (a server tick handler) is not it, and when X has none the action happens outside the repository.
# Other verbs ("opened", "applied", "registered") are left alone: the code that does them is seldom named
# after the thing, so a name match would refuse right answers.
_CALLBACK_VERBS = {"tick": "tick", "ticked": "tick", "render": "render", "draw": "draw", "drawn": "draw",
                   "paint": "paint", "painted": "paint", "repainted": "paint", "mounted": "mount",
                   "unmounted": "unmount"}
_ACTION_EN = re.compile(
    r"\bwhere\s+(?:is|are|was|were)\s+(?P<subj>.+?)\s+(?:being\s+|getting\s+)?(?P<verb>" + "|".join(_CALLBACK_VERBS)
    + r")\s*\??\s*$|\bwhere\s+(?:does|do|did)\s+(?P<subj2>.+?)\s+(?:get\s+)?(?P<verb2>"
    + "|".join(v for v in _CALLBACK_VERBS if not v.endswith(("ed", "wn"))) + r")\s*\??\s*$", re.I)
_ACTION_TR = re.compile(r"^(?P<subj>.+?)\s+nerede\s+(?P<verb>tick|render)['’](?:l[ae]n|l[ae]s|l[ae])\w*", re.I)
_ANAPHORS = frozenset("it its this that these those they them their".split())
_ARTICLES = frozenset("the a an our my your".split())


_INVERTED_USE = re.compile(r"\b(?:which|what)\b[^?]{0,60}?\b(?:do|does|did)\s+(?!not\b)[\w.$'-]+(?:\s+[\w.$'-]+){0,3}?"
                           r"\s+(?:use|call|read|import|invoke|depend on)\b")


def usage_question(text: str) -> bool:
    """Does ``text`` ask which code uses or calls something (rather than where it is)? "Which config does
    apply_discount read / use?" asks what the named code uses, not who uses it."""
    low = tn.nfc(text or "").lower()
    if _INVERTED_USE.search(low):
        return False
    if _USAGE_EN.search(low):
        return True
    return bool(_USAGE_TR.search(tn.fold_tr(tn.nfc(text or ""))))


def action_question(text: str) -> tuple[list[str], str] | None:
    """``(subject words, verb stem)`` for "where is X ticked?" (a lifecycle callback of X), else None.
    A subject that points back ("those", "it") is not read: what it names is not in the words."""
    raw = tn.nfc(text or "").strip()
    m = _ACTION_EN.search(raw)
    if m:
        subj, verb = (m.group("subj"), m.group("verb")) if m.group("verb") else (m.group("subj2"), m.group("verb2"))
        verb = _CALLBACK_VERBS[verb.lower()]
    else:
        m = _ACTION_TR.search(raw)
        if not m:
            return None
        subj, verb = m.group("subj"), m.group("verb").lower()
    toks = [t.strip("`'\"").lower() for t in tn.raw_tokens(subj)]
    if not toks or any(t in _ANAPHORS for t in toks):
        return None
    words: list[str] = []
    for tok in tn.raw_tokens(subj):  # `EmberForgeBlockEntity` -> ember, forge, block, entity
        tok = tok.strip("`'\"")
        camel = re.search(r"[a-z0-9][A-Z]", tok) or "_" in tok.strip("_")
        for w in tn.words(" ".join(tn.split_identifier(tok)) if camel else tok):
            if w not in _ARTICLES and w not in words:
                words.append(w)
    if not words or len(words) > 5:
        return None
    return words, verb


_MEMBERSHIP = (r"listed|registered|used|called|referenced|declared|included|imported|exported|tested|covered|"
               r"mentioned|defined|handled|documented|configured|present|found|in|part of|reachable|reached|wired|"
               r"applied|loaded|mapped|translated")
_ABSENT = r"missing|absent|unused|unlisted|unregistered|undeclared|untested|uncovered|unreferenced|orphaned"
_SETDIFF_EN = re.compile(
    rf"\b(?:are|is|were|was|do|does|did|have|has|got)\s*(?:not|n't|never)\s+(?:yet\s+)?(?:been\s+)?(?:{_MEMBERSHIP})\b"
    rf"|\b(?:aren't|isn't|don't|doesn't|haven't|hasn't|never)\s+(?:yet\s+)?(?:be(?:en)?\s+)?(?:{_MEMBERSHIP})\b"
    rf"|\b(?:are|is|were|was)\s+(?:still\s+)?(?:{_ABSENT})\b(?!-)|\b(?:missing|absent)\s+(?:from|in)\b"
    rf"|\b(?:which|what)\s+(?!(?:is|are|was|were|does|do|did|has|have)\b)(?:[\w-]+\s+){{0,2}}(?:{_ABSENT})\b(?!-)"
    rf"|\b(?:lack|lacks)\b|\b(?:have|has)\s+no\b")
_ENUM_EN = re.compile(r"^\W*(?:which|what|list|show|name|find|give|are there)\b|\b(?:which|what)\s+\w+s\b")
_CONDITIONAL_EN = re.compile(r"\b(?:if|when|unless|whether|what happens|why)\b")
_SETDIFF_TR = re.compile(r"\b\w+m[ae]y[ae]n\b|\b\w+m[ae]m[iu]s\w*|\beksik\w*|\bolmayan\w*|\bdegil\b")
_ENUM_TR = re.compile(r"\bhangi\w*|\bneler\w*|\blistele\w*")
_WHY_TR = re.compile(r"\bneden\b|\bnicin\b|\bniye\b")


_RELATIVE_EN = re.compile(r"(?:\bthat|\bwho|\bwhose|\bwhere|(?<=\w)\s+which)\s*$")
_TR_NEG_PREDICATE = re.compile(r"(?:\w+m[ae]m[iu]s\w*|\w+m[iu]yor\w*|\beksik\w*|\byok\w*|\bdegil\w*|\bolmayan\w*"
                               r"|\w+m[ae]y[ae]n(?:lar|ler)?)\s*[?.!]*\s*$")
_TR_NEG_HEAD = re.compile(r"\b(?:\w+m[ae]y[ae]n|\w+m[ae]m[iu]s|eksik\s+olan|olmayan)(?:\s+\w+){0,2}\s+(?:\w+l[ae]r\w*|hangi\w*|neler\w*)\s*[?.!]*\s*$")


def _relative_on_head(before: str) -> bool:
    """Does the relative clause ending ``before`` attach to the enumerated head ("list the files which ...",
    "which classes that ...") rather than to a later object ("which files call functions that ...")?"""
    m = re.search(r"\b(?:which|what|list|show|name|find|give)\b(.*)$", before)
    if not m:
        return False
    between = [w for w in re.findall(r"[\w'-]+", m.group(1)) if w not in ("the", "a", "an", "all", "of")]
    return len(between) <= 2  # the head noun and the relative pronoun


def asks_set_difference(text: str) -> str | None:
    """The words that make ``text`` ask for a set difference ("which X are not listed in Y"), else None.

    Only when the negation is the question's own predicate: "which files call functions that are not tested"
    asks for callers (the negation sits in a relative clause), "hangi dosya test edilmeyen kodu çağırıyor" too.
    In Turkish the negated predicate ends the question ("hangi mixinler listelenmemiş?", "hangileri eksik?") or
    a negated participle names the enumerated head ("kullanılmayan metodlar hangileri?")."""
    raw = tn.nfc(text or "").replace("’", "'")
    low = raw.lower()
    for m in _SETDIFF_EN.finditer(low):
        before = low[:m.start()]
        if not _ENUM_EN.search(low) or _CONDITIONAL_EN.search(before):
            continue
        if _RELATIVE_EN.search(before.rstrip()) and not _relative_on_head(before):
            continue  # "which files call functions that are not tested": the negation is about the object
        return m.group(0).strip()
    folded = tn.fold_tr(raw)
    if _ENUM_TR.search(folded) and not _WHY_TR.search(folded) and _SETDIFF_TR.search(folded):
        m = _TR_NEG_PREDICATE.search(folded) or _TR_NEG_HEAD.search(folded)
        if m:
            return m.group(0).strip(" ?.!")
    return None


# -- copies, reference trees and vendored folders --------------------------------------------------------------

def copy_roots(repo: Path, question: str) -> tuple[str, ...]:
    """Path prefixes of reference trees, detected copies and vendored folders the question does not name."""
    try:
        from verinoda import search_index as si

        roots = si._reference_roots(Path(repo), SimpleNamespace(words=tn.words(question or "")), question or "")
    except Exception:  # noqa: BLE001 - an unreadable config or copies file: no roots
        roots = ()
    return tuple(r for r in roots if r)


def in_copy(path: str, roots: tuple[str, ...]) -> bool:
    p = (path or "").replace("\\", "/")
    if not p:
        return False
    if any(p.startswith(r) for r in roots):
        return True
    return bool(set(PurePosixPath(p).parts[:-1]) & VENDORED_DIRS)


def copy_root_of(path: str, roots: tuple[str, ...]) -> str:
    """The reference / copy root or vendored folder ``path`` lies in (``reference/``, ``vendor/``)."""
    p = (path or "").replace("\\", "/")
    hit = next((r for r in roots if p.startswith(r)), None)
    if hit:
        return hit
    parts = PurePosixPath(p).parts[:-1]
    for i, part in enumerate(parts):
        if part in VENDORED_DIRS:
            return "/".join(parts[:i + 1]) + "/"
    return p


def cited_files(claim: dict, evidence: list[dict]) -> list[str]:
    """Repository files a claim cites: its source evidence and, for a relation, the call site."""
    out: list[str] = []
    for e in evidence:
        if e.get("source_type") in ("source_code", "design_doc", "config") and e.get("locator"):
            path = str(e["locator"]).split(" ")[0].rpartition(":")[0] or str(e["locator"])
            out.append(path)
    at = (claim.get("spec") or {}).get("at")
    if at and ":" in str(at):
        out.append(str(at).rpartition(":")[0])
    if not out:
        subs = claim.get("subjects") or []
        out = [str(s).split("::", 1)[0] for s in subs if s]
    return list(dict.fromkeys(p for p in out if p))


# -- matching words against code -----------------------------------------------------------------------------

def _stem(word: str) -> str:
    w = tn.en_stem(tn.fold_tr(word))
    for suf in ("ed", "en", "ing"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    if len(w) > 4 and w.endswith("i"):  # applied -> appli -> appl (apply)
        w = w[:-1]
    return w


def word_in(word: str, parts: list[str]) -> bool:
    """``word`` (a question word) is one of the identifier ``parts`` (stem-prefix match)."""
    s = _stem(word)
    if len(s) < 3:
        return False
    k = min(len(s), 5)
    return any(len(p) >= 3 and (p.startswith(s[:k]) or (len(p) >= 4 and s.startswith(p))) for p in parts)


def ident_parts(*names: str) -> list[str]:
    out: list[str] = []
    for n in names:
        out += tn.split_identifier(str(n or ""))
    return out


# -- unresolved call sites ---------------------------------------------------------------------------------------

_COMMENT_START = ("#", "//", "*", "/*", "--", "<!--")
_DECL_KEYWORD = re.compile(r"\b(?:def|function|fun|func|fn|sub|method)\s+$")
_DECL_MODIFIERS = re.compile(r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:(?:public|private|protected|static|final|abstract|"
                             r"synchronized|native|override|open|internal|suspend|default|inline|virtual|export)\s+)+")


def _in_string_or_comment(line: str, pos: int) -> bool:
    before = line[:pos]
    if before.count('"') % 2 or before.count("'") % 2 or before.count("`") % 2:
        return True
    for mark in ("#", "//"):
        i = before.find(mark)
        if i >= 0 and not (mark == "#" and before[i - 1:i] in ("&", "$")):
            return True
    return False


def _site_patterns(name: str) -> list[re.Pattern]:
    n = re.escape(name)
    return [re.compile(rf"(?<![\w$]){n}\s*\("),               # a call: name( / .name(
            re.compile(rf"::{n}\b"),                            # a method reference: Cls::name
            re.compile(rf"[(,]\s*(?:[\w$]+\.)*{n}\s*[,)]"),     # a callback argument: on("x", name)
            re.compile(rf"=\s*\{{\s*(?:[\w$]+\.)*{n}\s*\}}")]   # a JSX handler: onClick={name}


def _is_declaration(line: str, start: int, name: str) -> bool:
    before = line[:start]
    if _DECL_KEYWORD.search(before):
        return True
    stripped = line.strip()
    if _DECL_MODIFIERS.match(line) and not re.search(r"[=(]", before.split(name)[0] if name in before else before):
        return True
    # a JS/TS class method: `name(args) {`
    return bool(re.match(rf"^\s*(?:async\s+|static\s+|get\s+|set\s+)*{re.escape(name)}\s*\([^()]*\)\s*(?::[^={{]+)?"
                         rf"\{{\s*$", stripped))


def _target_kind(g, repo: Path, t: str) -> str:
    """``static`` (a Java/C# static member, a Kotlin object member), ``method`` or ``function``."""
    owner = next((u for u, _ in g.in_edges(t, {"method"})), None)
    if owner is None:
        return "function"
    f, ln = g.file(t), g.line(t) or 1
    try:
        lines = (Path(repo) / (f or "")).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "method"
    if re.search(r"\bstatic\b", " ".join(lines[max(0, ln - 3):ln])):
        return "static"
    oln = g.line(owner) or 0
    if (f or "").endswith((".kt", ".kts")) and 0 < oln <= len(lines) and re.search(r"\bobject\b", lines[oln - 1]):
        return "static"
    return "method"


_RECV_NAME = re.compile(r"([\w$]+)\s*(?:\?\.|\.|::)\s*$")
_RECV_NEW = re.compile(r"\bnew\s+([\w$.]+)\s*(?:<[^<>]*>)?\s*\([^()]*\)\s*\.\s*$")


def _receiver_rules_out(before: str, kind: str, owners: set[str], own_file: bool, file_defines: bool,
                        static_import: bool) -> bool:
    """Is the call whose name follows ``before`` certainly not a call of the target?"""
    b = before.rstrip()
    m = _RECV_NAME.search(b)
    if m:
        recv = m.group(1)
        if recv[:1].isupper() and recv not in owners and recv not in ("Object", "Self"):
            return True  # `Other.name(`: a static member of another class
        if kind == "static":
            return recv not in owners
        if kind == "function":
            return recv in ("self", "this", "cls", "super")
        return False
    if b.endswith((".", "?.", "::")):  # the result of a call or a constructor
        n = _RECV_NEW.search(b)
        if n and n.group(1).rpartition(".")[2] not in owners:
            return True
        return kind in ("static", "function")
    # a bare name: a static member outside its class needs a static import; in another file that
    # defines the same name, a method's bare call is that file's own one
    if kind == "static":
        return not own_file and not static_import
    return kind == "method" and not own_file and file_defines


def unresolved_call_sites(g, repo: Path, targets: list[str], *, skip_roots: tuple[str, ...] = (),
                          seconds: float = SCAN_SECONDS) -> dict:
    """Places in the indexed code files that write a target's name as a call, a method reference or a
    callback argument, but that the graph did not tie to any definition of that name.

    ``{"name", "sites": [(file, line, text)], "files": n scanned, "complete": bool, "kind"}`` (the first
    target's name; targets are callees of one question, normally one name). A receiver that cannot be the
    target (another class, ``self`` for a module function, an instance for a static member) is not counted."""
    from verinoda import callsite, retrieval

    targets = [t for t in targets if t in g.G]
    names = [callsite.target_token(g.label(t)) for t in targets]
    names = [n for n in dict.fromkeys(names) if n and len(n) >= 3 and re.fullmatch(r"[A-Za-z_$][\w$]*", n)]
    if not names:
        return {"name": None, "sites": [], "files": 0, "complete": True, "kind": None}
    name = names[0]
    targets = [t for t in targets if callsite.target_token(g.label(t)) == name]
    same = [n for n in g.G if g.file(n) and callsite.target_token(str(g.label(n))) == name]
    def_lines = {(g.file(n), g.line(n)) for n in same}
    def_files = {g.file(n) for n in same}
    resolved_at: set[str] = set()
    callers: set[str] = set()
    for n in same:
        for u, d in g.in_edges(n, {"calls", "uses", "references", "inherits", "implements"}):
            at = retrieval._at(d)
            if at:
                resolved_at.add(at)
            callers.add(u)
    owners = {qp_bare(g.label(u)) for t in targets for u, _ in g.in_edges(t, {"method", "contains"})}
    owners |= {PurePosixPath(g.file(t) or "").stem for t in targets}
    owners.discard("")
    own_files = {g.file(t) for t in targets}
    kind = _target_kind(g, repo, targets[0])
    static_rx = re.compile(r"\bimport\s+static\s+[\w.]*\b(?:" + "|".join([re.escape(name)] + [re.escape(o) + r"\.\*"
                                                                                               for o in owners]) + ")")
    files = sorted({f for _n, d in g.G.nodes(data=True) if (f := d.get("source_file"))
                    and PurePosixPath(f).suffix.lower() in CODE_SUFFIXES and not in_copy(f, skip_roots)})
    pats = _site_patterns(name)
    sites: list[tuple[str, int, str]] = []
    started, scanned, read, complete = time.monotonic(), 0, 0, True
    for f in files:
        # the limits are counts, so the same tree gives the same answer; the clock is a last resort
        if scanned >= SCAN_MAX_FILES or read >= SCAN_MAX_TOTAL or time.monotonic() - started > seconds:
            complete = False
            break
        p = Path(repo) / f
        try:
            size = p.stat().st_size
            if size > SCAN_MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        read += size
        if name not in text:
            continue
        static_import = bool(static_rx.search(text))
        for i, line in enumerate(text.splitlines(), 1):
            if name not in line or line.lstrip().startswith(_COMMENT_START):
                continue
            hits = sorted({m.start() for pat in pats for m in pat.finditer(line)})
            own_def = (f, i) in def_lines
            real = []
            for k, pos in enumerate(hits):
                at = line.find(name, pos)
                if _in_string_or_comment(line, at):
                    continue
                if (own_def and k == 0) or _is_declaration(line, at, name):
                    continue
                if _receiver_rules_out(line[:at], kind, owners, f in own_files, f in def_files, static_import):
                    continue
                real.append(at)
            if not real:
                continue
            if f"{f}:{i}" in resolved_at:
                continue
            encl = [s for s in g.symbols_in(f) if (sp := g.span(s)) and sp[0] <= i <= sp[1]]
            if any(s in callers for s in encl):
                continue  # the function around it has a call edge into a definition of this name
            sites.append((f, i, line.strip()[:120]))
    return {"name": name, "sites": sites, "files": scanned, "complete": complete, "kind": kind}


def qp_bare(label: str) -> str:
    return str(label or "").strip().lstrip(".").split("(")[0].rpartition(".")[2]


# -- the gate ----------------------------------------------------------------------------------------------------

_REASON = re.compile(r"\b(?:because|since|so that|so as to|to avoid|to prevent|to keep|to make sure|to ensure|"
                     r"in order to|due to|otherwise|as a result|reason)\b"
                     r"|\b(?:cunku|yuzunden|nedeniyle|sayesinde|diye)\b")
_CONFIG_GENERIC = frozenset("environment variable variables env config configuration configure configures configured "
                            "setting settings option options flag flags value values set sets read reads which what how "
                            "control controls controlled decide decides decided determine determines limit limits "
                            "ortam degisken degiskeni degiskenleri ayar ayari ayarlari yapilandirma".split())


def _add_unknown(res: dict, s: dict, u: dict) -> None:
    u = {**u, "sub_question": s["id"]}
    s.setdefault("unknowns", []).append(u)
    res.setdefault("unknowns", []).append(u)


def _claim_symbol(c: dict) -> str:
    spec = c.get("spec") or {}
    if spec.get("symbol"):
        return str(spec["symbol"])
    m = re.match(r"^`([^`]+)`", c.get("text") or "")
    return m.group(1) if m else ""


def _claim_file(c: dict) -> str:
    subs = c.get("subjects") or []
    if subs:
        return str(subs[0]).split("::", 1)[0]
    m = re.search(r"\b([\w./-]+\.\w+):\d+", c.get("text") or "")
    return m.group(1) if m else ""


def _node_of(g, file: str, symbol: str) -> str | None:
    bare = qp_bare(symbol)
    for n in g.symbols_in(file):
        if qp_bare(g.label(n)) == bare:
            return n
    return None


def _uses_subject(g, node: str | None, subjects: set[str]) -> bool:
    if node is None or node not in g.G:
        return False
    return any(v in subjects for v, _ in g.out_edges(node, {"calls", "uses", "references", "imports_from",
                                                           "inherits", "implements"}))


def apply(ctx, sq: dict, rows: list[dict], flags: dict, s: dict, res: dict, gate: dict,
          answer_ids: list[str], *, step=None) -> None:
    """Set ``flags["weak"]`` / ``flags["capped"]`` for one sub-question and add the unknowns that say why.

    ``rows``: the sub-question's claims as stored; ``gate``: what its run recorded (the subject nodes);
    ``answer_ids``: the claims that answer it (:func:`verinoda.analysis.answer_claims`)."""
    g, repo = ctx.g, ctx.repo
    intent = sq.get("intent") or ""
    text = sq.get("text_user_lang") or sq.get("text") or ""
    en_text = sq.get("text") or text
    by_id = {c["id"]: c for c in rows}
    answers = [by_id[i] for i in answer_ids if i in by_id]
    if not answers and intent != "callers":
        return
    subjects = set(gate.get("subjects") or [])
    weak: dict[str, str] = {}
    capped: list[str] = []

    # copies: evidence only in a reference tree, a copy of the project or a vendored folder
    roots = copy_roots(repo, text + " " + en_text)
    ev_cache: dict[str, list[dict]] = {}

    def evidence(cid: str) -> list[dict]:
        if cid not in ev_cache:
            try:
                ev_cache[cid] = ctx.rec.cl.evidence(cid)
            except Exception:  # noqa: BLE001 - no evidence rows: the claim's subjects stand in
                ev_cache[cid] = []
        return ev_cache[cid]

    copied: list[str] = []
    for c in answers:
        files = cited_files(c, evidence(c["id"]))
        if files and all(in_copy(f, roots) for f in files):
            weak[c["id"]] = "copy"
            copied.append(copy_root_of(files[0], roots))

    # relevance
    usage = usage_question(text) or usage_question(en_text)
    action = action_question(en_text) or action_question(text)
    for c in answers:
        if c["id"] in weak:
            continue
        kind = c.get("kind")
        if kind == "location":
            if intent not in LOCATION_ANSWERS:
                weak[c["id"]] = "definition"
            elif intent == "config":
                continue
            elif usage:
                node = _node_of(g, _claim_file(c), _claim_symbol(c))
                if not _uses_subject(g, node, subjects):
                    weak[c["id"]] = "definition"
            elif action:
                words, verb = action
                sym = _claim_symbol(c)
                file_parts = ident_parts(PurePosixPath(_claim_file(c)).stem)
                if sym:
                    has_subj = all(word_in(w, ident_parts(sym) + file_parts) for w in words)
                    has_verb = word_in(verb, ident_parts(sym))
                else:  # "file:line contains: ..." - the line's own words
                    line_words = tn.words((c.get("text") or "").partition(" contains: ")[2])
                    has_subj = all(word_in(w, line_words + file_parts) for w in words)
                    has_verb = word_in(verb, line_words + file_parts)
                if not (has_subj and has_verb):
                    weak[c["id"]] = "action"
        elif kind == "relation" and intent == "callers" and subjects:
            target = (c.get("spec") or {}).get("target")
            if target and target not in subjects:
                weak[c["id"]] = "callee"
        elif kind == "config":
            # the question's words and their expansions (a Turkish word's English code words included)
            raw_words = tn.words(en_text) + [str(x) for x in gate.get("words") or []]
            words = [w for w in dict.fromkeys(raw_words + ident_parts(*raw_words))
                     if w not in _CONFIG_GENERIC and len(w) >= 3]
            if words:
                spec = c.get("spec") or {}
                parts = ident_parts(spec.get("env") or "", _claim_symbol(c), PurePosixPath(_claim_file(c)).stem)
                if not any(word_in(w, parts) for w in words):
                    weak[c["id"]] = "config"
        elif kind == "history" and intent == "why":
            subj_line = (c.get("text") or "").rpartition("): ")[2]
            if not _REASON.search(tn.fold_tr(subj_line)):
                weak[c["id"]] = "commit"

    strong = [c for c in answers if c["id"] not in weak]
    notes = []
    if copied and not strong:
        notes.append({"question": sq.get("text") or "",
                      "why": f"the claims that answer it cite only {', '.join(sorted(set(copied)))}: a reference tree, "
                             "a copy of the project or a vendored folder, not the project's own code",
                      "next_step": "name that tree in the question if it is what you mean, or ask about the "
                                   "project's own code by name"})
    kinds = {weak[c["id"]] for c in answers if c["id"] in weak}
    if not strong and "definition" in kinds and usage:
        named = sorted({qp_bare(g.label(n)) for n in subjects if n in g.G and g.is_symbol(n)})
        what = " / ".join(f"`{n}`" for n in named) if 0 < len(named) <= 2 else "the code it names"
        notes.append({"question": sq.get("text") or "",
                      "why": f"the question asks which code uses {what}; the claims found are definitions, not code "
                             "that uses it",
                      "next_step": f"ask `what calls {named[0]}?`" if len(named) == 1 else
                                   "ask `what calls <name>?` for the function or class you mean"})
    elif not strong and "definition" in kinds and intent not in LOCATION_ANSWERS:
        notes.append({"question": sq.get("text") or "",
                      "why": "the claims found say where code is defined; that does not answer this question",
                      "next_step": "ask about one function by name (what it calls, what calls it)"})
    if not strong and "action" in kinds and action:
        words, verb = action
        notes.append({"question": sq.get("text") or "",
                      "why": f"no member of '{' '.join(words)}' named for '{verb}' was found: the claims are about "
                             f"other code, or name '{' '.join(words)}' without a '{verb}' of its own",
                      "next_step": f"if it has none, the '{verb}' comes from outside the repository (a superclass, a "
                                   "framework or a game loop): check its class hierarchy"})
    if not strong and "config" in kinds:
        notes.append({"question": sq.get("text") or "",
                      "why": "the configuration reads found do not carry the question's words (variable, reader "
                             "or file); they may be unrelated",
                      "next_step": "name the setting or the function the question is about"})
    if intent == "why" and "commit" in kinds and not strong:
        n = sum(1 for c in answers if weak.get(c["id"]) == "commit")
        capped.append("why: no reason found")
        notes.append({"question": sq.get("text") or "",
                      "why": f"the reason was not found: {n} commit line(s) say when the code changed, not why, and "
                             "no decision record, comment or document found states it",
                      "next_step": "read the comments and docs around the code, or ask its author; record the "
                                   "answer with `verinoda decide record`"})

    # callers: call sites of the callee the graph did not resolve
    if intent == "callers" and not flags.get("not_found") and strong:  # no strong claim: a cap changes nothing
        named = [n for n in gate.get("subjects") or [] if n in g.G and g.is_symbol(n)]
        claimed = [t for t in dict.fromkeys((c.get("spec") or {}).get("target") for c in answers
                                            if c.get("kind") == "relation" and weak.get(c["id"]) != "callee") if t]
        targets = [t for t in (named or claimed) if t in g.G][:2]
        if targets:
            scan = unresolved_call_sites(g, repo, targets, skip_roots=roots)
            if step is not None:
                step("call_sites", f"{scan['name']}: {len(scan['sites'])} unresolved in {scan['files']} file(s)"
                     + ("" if scan["complete"] else " (stopped at its limit)"))
            if scan["sites"]:
                n = len(scan["sites"])
                shown = ", ".join(f"{f}:{ln}" for f, ln, _ in scan["sites"][:SITES_SHOWN])
                capped.append(f"callers: {n} call sites unresolved")
                label = qp_bare(g.label(targets[0]))
                notes.append({"question": f"which other code calls {label}?",
                              "why": f"{n} call site{'s' if n != 1 else ''} unresolved: {shown}"
                                     + (", ..." if n > SITES_SHOWN else "")
                                     + f" - these lines call or pass `{scan['name']}` (through an object, a "
                                       "callback or a listener), and the graph did not tie them to "
                                       f"{label} or to any other definition of that name; the callers listed may be "
                                       "incomplete"
                                     + ("" if scan["complete"] else "; the search stopped at its limit, so there "
                                                                    "may be more"),
                              "next_step": f"read those lines; `verinoda resolve-call FILE:LINE {scan['name']}` "
                                           "resolves one with the precise resolver when it is installed"})
            elif not scan["complete"]:
                capped.append("callers: call-site search incomplete")
                notes.append({"question": f"which other code calls {qp_bare(g.label(targets[0]))}?",
                              "why": f"the search for other call sites stopped after {scan['files']} file(s) (its "
                                     "limit); calls the graph did not resolve may be missing",
                              "next_step": f"search the code for '{scan['name']}('"})
    if weak:
        flags[WEAK] = sorted(set(flags.get(WEAK) or []) | set(weak))
        copies = [i for i, why in weak.items() if why == "copy"]
        if copies:
            flags[WEAK_COPY] = sorted(set(flags.get(WEAK_COPY) or []) | set(copies))
    if capped:
        flags[CAPPED] = list(dict.fromkeys((flags.get(CAPPED) or []) + capped))
    for u in notes:
        _add_unknown(res, s, u)
