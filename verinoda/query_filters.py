"""Filter syntax in a query (docs/DESIGN.md, "Filter syntax in a query"): ``path:``, ``lang:``, ``symbol:``,
``is:``, ``/regex/`` and ``AND`` / ``OR`` / ``NOT`` narrow the units the ranked text search returns.

``verinoda query`` and MCP ``project_query`` read it; other callers of :func:`verinoda.retrieval.retrieve` pass
questions through unchanged. A question with no filter atom is not parsed at all (an upper-case ``OR`` in plain
prose stays a word), so every question that worked before ranks as before.

* ``path:GLOB`` - the file's repository path, case-insensitive. A glob (``*``, ``?``, ``[..]``; ``**/`` also
  matches no folder) matches the whole path from the root; ``*`` crosses folders, so ``*.py`` is every Python
  file. A plain value matches from the root or after any ``/`` (``path:search_index``, ``path:tests/``,
  ``path:shop/cache.py``). ``file:`` is the same filter.
* ``lang:NAME`` - the file's language by suffix (``python``/``py``, ``java``, ``kotlin``/``kt``, ``ts``,
  ``js``, ...); an unknown name is a suffix (``lang:mcfunction``). ``language:`` is the same filter.
* ``symbol:NAME`` - a symbol unit whose name or qualified name is NAME (``take``, ``Budget.take``), or matches
  it as a glob (``symbol:test_*``). NAME is also ranked text, so the exact-identifier boost applies.
* ``is:vendored`` - vendored, minified or generated code (the extractor's rules,
  ``project_index.vendored.vendored_reason``); ``is:generated``, ``is:minified`` and ``is:test`` name one kind.
* ``/regex/`` - a line of the unit matches (Python syntax, one line at a time; ``/.../i`` ignores case). The
  first matching line is the item's evidence (``regex /x/ at file:N``). A token with a ``/`` inside
  (``/api/users/``) is a word, not a regex.
* Atoms side by side must all hold; ``OR`` joins alternatives, ``NOT`` or a leading ``-`` negates, parentheses
  group (``(path:a OR path:b) NOT is:test``). A plain word next to an operator (``cache OR memo``,
  ``NOT legacy``) is a content atom: a line of the unit holds it, any case, also inside an identifier. Every
  other plain word is ranked text. ``AND`` binds tighter than ``OR``: ``a OR b path:x`` is ``a OR (b path:x)``.

A question of filters only (no ranked text) lists every unit that matches, in path and line order.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from verinoda.index import file_lines
from verinoda.project_index.vendored import vendored_reason
from verinoda.testcode import is_test_file

KEYS = {"path": "path", "file": "path", "lang": "lang", "language": "lang", "symbol": "symbol", "is": "is"}
IS_VALUES = ("vendored", "generated", "minified", "test")
LANGS: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi"), "java": (".java",), "kotlin": (".kt", ".kts"), "scala": (".scala",),
    "groovy": (".groovy", ".gradle"), "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "typescript": (".ts", ".tsx", ".mts", ".cts"), "go": (".go",), "rust": (".rs",), "csharp": (".cs",),
    "c": (".c", ".h"), "cpp": (".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"), "ruby": (".rb",), "php": (".php",),
    "swift": (".swift",), "lua": (".lua",), "shell": (".sh", ".bash"), "sql": (".sql",), "glsl": (".glsl", ".vsh",
    ".fsh", ".vert", ".frag"), "markdown": (".md", ".markdown", ".mdx"), "json": (".json", ".jsonc", ".json5"),
    "yaml": (".yml", ".yaml"), "toml": (".toml",), "xml": (".xml",), "html": (".html", ".htm"), "css": (".css",),
}
LANG_ALIASES = {"py": "python", "kt": "kotlin", "js": "javascript", "ts": "typescript", "rs": "rust",
                "cs": "csharp", "c#": "csharp", "c++": "cpp", "rb": "ruby", "sh": "shell", "bash": "shell",
                "md": "markdown", "yml": "yaml", "jvm": "jvm"}
JVM = ("java", "kotlin", "scala", "groovy")
OPERATORS = {"AND", "OR", "NOT"}
_GLOB = re.compile(r"[*?\[]")
_KEYED = re.compile(r"(-?)([A-Za-z]+):(\"[^\"]*\"|\S+)")


class FilterError(ValueError):
    """A filter the question writes but that cannot be read (unknown ``is:`` value, bad regex, dangling OR)."""


@dataclass(frozen=True)
class Atom:
    kind: str     # path | lang | symbol | is | regex | word
    value: str
    rx: re.Pattern | None = field(default=None, compare=False)

    def __str__(self) -> str:
        if self.kind == "regex":
            return f"/{self.value}/" + ("i" if self.rx is not None and self.rx.flags & re.IGNORECASE else "")
        return self.value if self.kind == "word" else f"{self.kind}:{self.value}"

    @property
    def cost(self) -> int:  # cheap predicates first in an AND: a regex reads the file
        return {"path": 0, "lang": 0, "symbol": 1, "is": 2}.get(self.kind, 3)


@dataclass
class Filters:
    """A parsed question: the text to rank and the filter expression (None: nothing to filter on)."""

    text: str
    expr: object          # Atom | ("and", [..]) | ("or", [..]) | ("not", x)
    atoms: list[Atom]

    def describe(self) -> str:
        return _show(self.expr)


def _show(e, top: bool = True) -> str:
    if isinstance(e, Atom):
        return str(e)
    op, arg = e
    if op == "not":
        return "NOT " + _show(arg, False)
    s = (" " if op == "and" else " OR ").join(_show(x, False) for x in arg)
    return s if top else f"({s})"


def _tokens(question: str) -> list[tuple[str, object]]:
    """(kind, value): LP, RP, OP (AND/OR/NOT), NEG (a leading '-'), ATOM, WORD."""
    out: list[tuple[str, object]] = []
    i, n = 0, len(question)
    while i < n:
        c = question[i]
        if c.isspace():
            i += 1
            continue
        if c in "()":
            out.append(("LP" if c == "(" else "RP", c))
            i += 1
            continue
        neg = c == "-" and i + 1 < n and not question[i + 1].isspace()
        j = i + 1 if neg else i
        if j < n and question[j] == "/":
            k = j + 1
            while k < n and question[k] != "/":
                k += 2 if question[k] == "\\" else 1
            # a regex ends at its second unescaped '/', then an optional 'i', then a space, ')' or the end
            m = re.match(r"i?(?=[\s)]|$)", question[k + 1:]) if k < n and k > j + 1 else None
            if m is not None:
                if neg:
                    out.append(("NEG", "-"))
                out.append(("ATOM", _regex(question[j + 1:k], bool(m.group(0)))))
                i = k + 1 + len(m.group(0))
                continue
        m = _KEYED.match(question, i)
        if m is not None and m.group(2).lower() in KEYS:
            key = KEYS[m.group(2).lower()]
            raw = m.group(3)
            value = raw[1:-1] if raw.startswith('"') else raw
            # a closing parenthesis glued to the value belongs to the grouping
            tail = len(value) - len(value.rstrip(")")) if not raw.startswith('"') else 0
            value = value[:len(value) - tail] if tail else value
            if value:
                if m.group(1):
                    out.append(("NEG", "-"))
                out.append(("ATOM", _keyed(key, value)))
                i = m.end() - tail
                continue
        k = i
        while k < n and not question[k].isspace() and question[k] not in "()":
            k += 1
        w = question[i:k]
        out.append(("OP", w) if w in OPERATORS else ("WORD", w))
        i = k
    return out


def _regex(body: str, nocase: bool) -> Atom:
    try:
        rx = re.compile(body, re.IGNORECASE if nocase else 0)
    except re.error as exc:
        raise FilterError(f"/{body}/ is not a regular expression: {exc}") from None
    return Atom("regex", body, rx)


def _keyed(key: str, value: str) -> Atom:
    v = value.lower() if key in ("lang", "is") else value
    if key == "is" and v not in IS_VALUES:
        raise FilterError(f"is:{value} is not a filter (is:{', is:'.join(IS_VALUES)})")
    return Atom(key, v)


def parse(question: str) -> Filters | None:
    """The filters a question writes, or None when it writes no filter atom (the question is then left as is)."""
    toks = _tokens(question)
    if not any(k == "ATOM" for k, _ in toks):
        return None
    stream: list[tuple[str, object]] = []
    text: list[str] = []
    for x, (k, v) in enumerate(toks):
        if k == "WORD":
            prev = toks[x - 1] if x else None
            nxt = toks[x + 1] if x + 1 < len(toks) else None
            beside = (prev is not None and prev[0] == "OP") or (nxt is not None and nxt[0] == "OP" and nxt[1] != "NOT")
            if beside:
                word = str(v).strip(".,;:!?\"'")
                if not word:
                    continue
                # a substring, so that `legacy` also meets price_of_legacy and LegacyPrice
                stream.append(("ATOM", Atom("word", word, re.compile(re.escape(word), re.IGNORECASE))))
                if prev != ("OP", "NOT"):
                    text.append(word)
            else:
                text.append(str(v))
            continue
        if k == "ATOM" and v.kind == "symbol" and not _GLOB.search(v.value):
            text.append(v.value)
        stream.append((k, v))
    pos = 0

    def peek():
        return stream[pos] if pos < len(stream) else None

    def take():
        nonlocal pos
        pos += 1
        return stream[pos - 1]

    def p_or():
        parts = [p_and()]
        while peek() == ("OP", "OR"):
            take()
            if peek() is None or peek()[0] in ("RP",) or peek() == ("OP", "OR"):
                raise FilterError("OR needs a filter on each side")
            parts.append(p_and())
        parts = [x for x in parts if x is not None]
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else ("or", parts)

    def p_and():
        parts = []
        while True:
            t = peek()
            if t is None or t[0] == "RP" or t == ("OP", "OR"):
                break
            if t == ("OP", "AND"):
                take()
                continue
            x = p_not()
            if x is not None:
                parts.append(x)
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else ("and", parts)

    def p_not():
        t = take()
        if t[0] == "NEG" or t == ("OP", "NOT"):
            if peek() is None or peek()[0] in ("RP", "OP") and peek() != ("OP", "NOT"):
                raise FilterError("NOT needs a filter after it")
            x = p_not()
            return None if x is None else ("not", x)
        if t[0] == "LP":
            x = p_or()
            if peek() is not None and peek()[0] == "RP":
                take()
            return x
        if t[0] == "RP":
            return None
        return t[1]

    expr = None
    while pos < len(stream):
        x = p_or()
        if x is not None:
            expr = x if expr is None else ("and", [expr, x])
        if pos < len(stream) and stream[pos][0] == "RP":
            pos += 1  # an unmatched ')' is ignored
    atoms = [v for k, v in stream if k == "ATOM"]
    return Filters(" ".join(text).strip(), expr, atoms)


def _cost(e) -> int:
    if isinstance(e, Atom):
        return e.cost
    return max(_cost(x) for x in e[1]) if e[0] in ("and", "or") else _cost(e[1])


def lang_matches(lang: str, file: str) -> bool:
    suffix = PurePosixPath(file).suffix.lower()
    name = LANG_ALIASES.get(lang, lang)
    if name == "jvm":
        return any(suffix in LANGS[x] for x in JVM)
    if name in LANGS:
        return suffix in LANGS[name]
    return suffix == "." + name.lstrip(".")


def path_matches(pattern: str, file: str) -> bool:
    p, f = pattern.lower().replace("\\", "/"), file.lower()
    p = p[2:] if p.startswith("./") else p.lstrip("/")
    if not _GLOB.search(p):
        return f.startswith(p) or "/" + p in f
    forms = {p, p.replace("**/", "")} if "**/" in p else {p}
    return any(fnmatch.fnmatchcase(f, x) for x in forms)


def symbol_matches(value: str, name: str, qual: str) -> bool:
    v, n, q = value.lower(), (name or "").lower(), (qual or "").lower()
    if _GLOB.search(v):
        return fnmatch.fnmatchcase(n, v) or fnmatch.fnmatchcase(q, v)
    return v in (n, q) or q.endswith("." + v)


class Selector:
    """Evaluates a :class:`Filters` expression on the index's units. Called as ``search_index.rank``'s ``where``:
    the units kept, each with its reasons and the line a regex or word atom matched (None when no line did)."""

    def __init__(self, filters: Filters, root: Path):
        self.f = filters
        self.root = Path(root)
        self._reason: dict[str, str | None] = {}
        self._vcache: dict = {}
        self._quals: dict[int, str] = {}

    def __call__(self, h, conn, uids: list[int]) -> dict[int, tuple[list[str], int | None]]:
        if self.f.expr is None:
            return {u: ([], None) for u in uids}
        if any(a.kind == "symbol" for a in self.f.atoms):
            want = [u for u in uids if h.units[u][2] == "symbol"]
            for k in range(0, len(want), 900):
                chunk = want[k:k + 900]
                self._quals.update(conn.execute(
                    f"SELECT uid, qual FROM units WHERE uid IN ({','.join('?' * len(chunk))})", chunk).fetchall())
        out: dict[int, tuple[list[str], int | None]] = {}
        for u in uids:
            ok, ev = self._eval(self.f.expr, h, u)
            if ok:
                if ev is None:
                    out[u] = ([], None)
                else:
                    atom, line = ev
                    what = f"regex {atom}" if atom.kind == "regex" else f"word '{atom}'"
                    out[u] = ([f"{what} at {h.units[u][0]}:{line}"], line)
        return out

    def _eval(self, e, h, u) -> tuple[bool, tuple[Atom, int] | None]:
        """Does unit ``u`` satisfy ``e``, and on which line (the first regex or word atom that matched a line)."""
        if isinstance(e, Atom):
            return self._atom(e, h, u)
        op, arg = e
        if op == "not":
            return not self._eval(arg, h, u)[0], None
        if op == "and":
            first = None
            for x in sorted(arg, key=_cost):  # cheap predicates first: a regex reads the file
                ok, ev = self._eval(x, h, u)
                if not ok:
                    return False, None
                first = first or ev
            return True, first
        for x in arg:
            ok, ev = self._eval(x, h, u)
            if ok:
                return True, ev
        return False, None

    def _atom(self, a: Atom, h, u) -> tuple[bool, tuple[Atom, int] | None]:
        file, _nid, kind, name, lo, hi = h.units[u][:6]
        if a.kind == "path":
            return path_matches(a.value, file), None
        if a.kind == "lang":
            return lang_matches(a.value, file), None
        if a.kind == "symbol":
            return kind == "symbol" and symbol_matches(a.value, name, self._quals.get(u, "")), None
        if a.kind == "is":
            if a.value == "test":
                return is_test_file(file), None
            why = self._why(file)
            return (why is not None if a.value == "vendored" else why == a.value), None
        lines = file_lines(self.root / file) or []
        for n in range(max(1, lo), min(hi, len(lines)) + 1):
            if a.rx.search(lines[n - 1]):
                return True, (a, n)
        return False, None

    def _why(self, file: str) -> str | None:
        if file not in self._reason:
            self._reason[file] = vendored_reason(self.root / file, self.root, self._vcache)
        return self._reason[file]
