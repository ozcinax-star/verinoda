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
* ``test_run``: full only when the claim spec names the run (``spec.experiment``
  or all ``spec.command`` ids in its command line) and every claim subject is
  exercised by it (test files in the command; code reached through the test
  files' in-repo imports).
* ``tests``, ``impact``: ``partial`` at most (static reachability is inference).
* ``decision`` / ``history``: ``partial`` at most - a document's rationale
  cannot be entailed mechanically. ``partial`` needs *attribution*: the record
  must state every content term the claim attributes to it (for history, the
  claim must name the commit); anything less is ``none``.
* anything else (``general``): term coverage. ``full`` when every key term
  (identifiers, numbers, product names such as ``PostgreSQL`` or ``AES-256``)
  and every content word of the claim appears in the evidence (cited lines,
  enclosing symbol, file name; for a run: its command and summary), the claim
  cites no other location, and it has no negation or quantifier ("not",
  "only", "all" ...) - text overlap cannot establish those. This grade is a
  labelled heuristic (``Grade.reason`` says so).
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
            return Grade("partial", "terms covered, but the claim cites another location (heuristic: term coverage)")
        if terms.negated or terms.quantified:
            return Grade("partial", "terms covered, but a negation/quantifier cannot be established by text "
                                    "(heuristic: term coverage)")
        return Grade("full", "every term of the claim appears in the evidence (heuristic: term coverage)")
    relevant = bool(cov["keys_hit"]) or len(cov["words_hit"]) >= 2 or \
        (not terms.keys and cov["words_hit"] and 2 * len(cov["words_hit"]) >= len(cov["words"]))
    if relevant:
        return Grade("partial", "evidence is about the claim's subject; missing: " + ", ".join(cov["missing"][:6]))
    return Grade("none", "evidence shares no key term with the claim"
                 + (f" (missing: {', '.join(cov['missing'][:6])})" if cov["missing"] else ""))


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
    return (label or "").strip().lstrip(".").split("(")[0].rpartition(".")[2].strip()


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
    a_path, a_sym = _subject_parts(subjects[0] if subjects else None)
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
        caller = a_sym or spec.get("source_label")
        if caller and re.search(r"\.[A-Za-z]{1,5}$", caller.strip()) and not caller.strip().endswith(")"):
            caller = None  # the caller is a module (file node): no enclosing def to check
        return _call_grade(repo, ev, token, caller=caller, target_path=b_path, target_qual=b_sym,
                           relation=spec.get("relation"))
    return _general(repo, ev, text)


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
                    return Grade("full", f"environment read of {var} ({_lang} pattern)")
    if re.search(rf"\b{re.escape(var)}\b", shown):
        return Grade("partial", f"{var} appears, but not as an environment read")
    return Grade("none", f"cited lines do not mention {var}")


def _is_env_read(node: ast.AST, var: str) -> bool:
    def is_environ(e: ast.AST) -> bool:
        return (isinstance(e, ast.Attribute) and e.attr == "environ") or (isinstance(e, ast.Name) and e.id == "environ")

    def lit(e: ast.AST | None) -> bool:
        return isinstance(e, ast.Constant) and e.value == var

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
    if isinstance(node, ast.Compare) and lit(node.left) and any(is_environ(c) for c in node.comparators):
        return True
    return False


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
        return _call_grade(repo, ev, _token(to), caller=_token(frm) or None, target_path=None, target_qual=None,
                           relation=relation)
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
        if len(q) >= 8 and q in " ".join((shown or "").split()):
            return Grade("full", "the cited lines contain the quoted text verbatim")
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


_PREDICATES = {
    "relation": _relation, "location": _location, "config": _config, "flow": _flow, "exclusive": _exclusive,
    "tests": _tests, "test_run": _test_run, "impact": _impact,
}
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
        try:
            _t, _a, _b, _full = _evidence_text(Path(repo) if repo else None, ev)
            ev_txt = _full or _t or ""  # key terms may sit in the enclosing def (the caller's name)
        except (OSError, ValueError, TypeError):
            ev_txt = ""
        terms = claim_terms(text)
        missing = coverage(Terms(terms.keys, [], [], [], False, False), ev_txt)["missing"]
        if missing:
            problems.append("key terms not in the cited file: " + ", ".join(missing[:4]))
        if problems:
            g = cap_grade(Grade(g.grade, g.reason + "; claim text: " + "; ".join(problems)), "partial")
    if key is not None:
        if len(_CACHE) > 4096:
            _CACHE.clear()
        _CACHE[key] = g
    return g


def grade(kind: str, repo: Path | str | None, spec: dict | None, evidence_row: dict, *,
          text: str | None = None, subjects: list[str] | None = None) -> str:
    """``"full" | "partial" | "none"`` for one evidence row (the contract other modules build on)."""
    return assess(kind, repo, spec, evidence_row, text=text, subjects=subjects).grade
