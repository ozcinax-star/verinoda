"""Filter syntax in a query (docs/DESIGN.md, "Filter syntax in a query"): ``path:``, ``lang:``, ``symbol:``,
``is:``, ``/regex/`` and ``AND`` / ``OR`` / ``NOT`` narrow the units the ranked text search returns.

``verinoda query`` and MCP ``project_query`` read it; other callers of :func:`verinoda.retrieval.retrieve` pass
questions through unchanged. A question with no filter atom is not parsed at all (an upper-case ``OR`` in plain
prose stays a word, and so do ``is:null`` and ``/health/``), so every such question ranks as before. With a
filter, the rest of the question is ranked as written, calls included (``parse()`` stays ``parse()``).

* ``path:GLOB`` - the file's repository path, case-insensitive. A glob (``*``, ``?``, ``[..]``; ``**/`` also
  matches no folder) matches the whole path from the root; ``*`` crosses folders, so ``*.py`` is every Python
  file. A plain value matches from the root or after any ``/`` (``path:search_index``, ``path:tests/``,
  ``path:shop/cache.py``). An absolute path inside the repository is read from its root. ``file:`` is the
  same filter.
* ``lang:NAME`` - the file's language by suffix (``python``/``py``, ``java``, ``kotlin``/``kt``, ``ts``,
  ``js``, ...); an unknown name is a suffix (``lang:mcfunction``). ``language:`` is the same filter.
* ``symbol:NAME`` - a symbol unit whose name or qualified name is NAME (``take``, ``Budget.take``), or matches
  it as a glob (``symbol:test_*``). NAME is also ranked text, so the exact-identifier boost applies.
* ``is:vendored`` - vendored, minified or generated code (the extractor's rules,
  ``project_index.vendored.vendored_reason``); ``is:generated``, ``is:minified`` and ``is:test`` name one kind.
  Another ``is:`` value is an error beside another filter, and a word otherwise.
* ``/regex/`` - a line of the unit matches (Python syntax, one line at a time; ``/.../i`` ignores case). The
  first matching line is the item's evidence (``regex /x/ at file:N``). A token with a ``/`` inside
  (``/api/users/``) or one path segment between slashes (``/health/``) is a word, not a regex; ``/TODO/i`` or
  ``/\\bTODO/`` search such a word. The search runs in a child process stopped after :data:`REGEX_SECONDS`.
* Atoms side by side must all hold; ``OR`` joins alternatives, ``NOT`` or a leading ``-`` negates (also a group,
  ``-(...)``), parentheses group (``(path:a OR path:b) NOT is:test``). A plain word next to an operator (``cache OR memo``,
  ``NOT legacy``) is a content atom: a line of the unit holds it, any case, also inside an identifier. Every
  other plain word is ranked text. ``AND`` binds tighter than ``OR``: ``a OR b path:x`` is ``a OR (b path:x)``.

A question of filters only (no ranked text) lists every unit that matches, in path and line order.
"""

from __future__ import annotations

import bisect
import fnmatch
import json
import re
import subprocess
import sys
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
    "cobol": (".cbl", ".cob", ".cobol", ".cpy"), "erlang": (".erl", ".hrl", ".escript"), "r": (".r",),
    "solidity": (".sol",), "vbnet": (".vb",),
}
LANG_ALIASES = {"py": "python", "kt": "kotlin", "js": "javascript", "ts": "typescript", "rs": "rust",
                "cs": "csharp", "c#": "csharp", "c++": "cpp", "rb": "ruby", "sh": "shell", "bash": "shell",
                "md": "markdown", "yml": "yaml", "jvm": "jvm", "vb": "vbnet", "vb.net": "vbnet", "erl": "erlang",
                "sol": "solidity", "cob": "cobol"}
JVM = ("java", "kotlin", "scala", "groovy")
OPERATORS = {"AND", "OR", "NOT"}
MAX_DEPTH = 32          # nested groups and chained NOTs
REGEX_SECONDS = 10.0    # a /regex/ runs in a child process, stopped after this long
_SEGMENT = re.compile(r"[\w.-]+")
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


def _tokens(question: str) -> tuple[list[tuple[str, object]], list[str]]:
    """(kind, value): LP, RP, OP (AND/OR/NOT), NEG (a leading '-'), ATOM, WORD; and the ``is:`` tokens whose
    value is not a filter (left as words)."""
    out: list[tuple[str, object]] = []
    unknown_is: list[str] = []
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
        if c == "-" and i + 1 < n and question[i + 1] == "(":
            out.append(("NEG", "-"))  # a negated group
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
            # one path segment between slashes (/health/, /tmp/) is a URL path or folder in prose
            if m is not None and not m.group(0) and _SEGMENT.fullmatch(question[j + 1:k]):
                m = None
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
            if value and key == "is" and value.lower() not in IS_VALUES:
                unknown_is.append(question[i:m.end() - tail])
            elif value:
                if value.startswith("(") and not raw.startswith('"'):
                    raise FilterError(f"{m.group(2)}:{value}: a filter value cannot open a group; write "
                                      f"({m.group(2)}:a OR {m.group(2)}:b)")
                if m.group(1):
                    out.append(("NEG", "-"))
                out.append(("ATOM", _keyed(key, value)))
                i = m.end() - tail
                continue
        # a word keeps the parentheses of a call written in it (parse(), foo(bar)): a '(' inside the word
        # and the ')' that closes it are part of the word, as the ranked text had them before
        k, depth = i, 0
        while k < n and not question[k].isspace():
            ch = question[k]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            k += 1
        w = question[i:k]
        out.append(("OP", w) if w in OPERATORS else ("WORD", w))
        i = k
    return out, unknown_is


def _regex(body: str, nocase: bool) -> Atom:
    try:
        rx = re.compile(body, re.IGNORECASE if nocase else 0)
    except re.error as exc:
        raise FilterError(f"/{body}/ is not a regular expression: {exc}") from None
    return Atom("regex", body, rx)


def _keyed(key: str, value: str) -> Atom:
    return Atom(key, value.lower() if key in ("lang", "is") else value)


def parse(question: str) -> Filters | None:
    """The filters a question writes, or None when it writes no filter atom (the question is then left as is)."""
    toks, unknown_is = _tokens(question)
    if not any(k == "ATOM" for k, _ in toks):
        return None  # an is:word in prose without any filter stays a word
    if unknown_is:
        raise FilterError(f"{unknown_is[0]} is not a filter (is:{', is:'.join(IS_VALUES)})")
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

    def operand_next() -> bool:
        t = peek()
        return t is not None and t[0] != "RP" and t not in (("OP", "OR"), ("OP", "AND"))

    def p_or(depth: int):
        parts = [p_and(depth)]
        while peek() == ("OP", "OR"):
            take()
            if parts[-1] is None or not operand_next():
                raise FilterError("OR needs a filter on each side")
            parts.append(p_and(depth))
        parts = [x for x in parts if x is not None]
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else ("or", parts)

    def p_and(depth: int):
        parts = []
        while True:
            t = peek()
            if t is None or t[0] == "RP" or t == ("OP", "OR"):
                break
            if t == ("OP", "AND"):
                take()
                if not parts or not operand_next():
                    raise FilterError("AND needs a filter on each side")
                continue
            x = p_not(depth)
            if x is not None:
                parts.append(x)
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else ("and", parts)

    def p_not(depth: int):
        if depth > MAX_DEPTH:
            raise FilterError(f"filters nest deeper than {MAX_DEPTH} levels")
        t = take()
        if t[0] == "NEG" or t == ("OP", "NOT"):
            if peek() is None or peek()[0] in ("RP", "OP") and peek() != ("OP", "NOT"):
                raise FilterError("NOT needs a filter after it")
            x = p_not(depth + 1)
            return None if x is None else ("not", x)
        if t[0] == "LP":
            x = p_or(depth + 1)
            if peek() is None or peek()[0] != "RP":
                raise FilterError("a '(' is not closed")
            take()
            return x
        if t[0] == "RP":
            return None
        return t[1]

    expr = None
    while pos < len(stream):
        x = p_or(0)
        if x is not None:
            expr = x if expr is None else ("and", [expr, x])
        if pos < len(stream) and stream[pos][0] == "RP":
            raise FilterError("a ')' closes no '('")
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


def repo_path(value: str, root: Path) -> str | None:
    """A ``path:`` value written as an absolute path inside the repository: the file or folder it names,
    relative to the root and lower-case ("" for the root itself). None for any other value; an absolute path
    outside the repository is a :class:`FilterError`."""
    v = value.replace("\\", "/")
    base = Path(root).resolve().as_posix().rstrip("/")
    if v.lower() == base.lower() or v.lower().startswith(base.lower() + "/"):
        return v[len(base) + 1:].strip("/").lower()
    if re.match(r"[A-Za-z]:/", v) or v.startswith("//"):
        raise FilterError(f"path:{value} is outside the repository ({base}); write a path from its root")
    return None


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


# A user's /regex/ runs in a child process (stdlib only, isolated) that is stopped after REGEX_SECONDS: Python's
# `re` has no time limit, and a pattern that backtracks badly on a long line would otherwise hang the query.
_REGEX_CHILD = r"""
import json, re, sys
from pathlib import Path
job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
root = Path(job["root"])
rxs = [re.compile(body, flags) for body, flags in job["patterns"]]
out = [{} for _ in rxs]
given = job["lines"]
for f in job["files"]:
    lines = given.get(f)
    if lines is None:
        try:
            lines = (root / f).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
    for i, rx in enumerate(rxs):
        hit = [n for n, s in enumerate(lines, 1) if rx.search(s)]
        if hit:
            out[i][f] = hit
sys.stdout.buffer.write(json.dumps(out).encode("ascii"))
"""


def regex_lines(root: Path, files: list[str], atoms: list[Atom],
                seconds: float | None = None) -> list[dict[str, list[int]]]:
    """For each regex atom, the lines (1-based) of each file that it matches, searched in a child process that
    is stopped after ``seconds`` (default :data:`REGEX_SECONDS`; a :class:`FilterError` then)."""
    from verinoda import doctext

    limit = REGEX_SECONDS if seconds is None else seconds
    root = Path(root)
    # a PDF or Office document is searched in its text view, which only this process can build
    lines = {f: file_lines(root / f) or [] for f in files if doctext.kind(PurePosixPath(f).name) is not None}
    job = {"root": str(root), "files": files, "lines": lines,
           "patterns": [[a.value, a.rx.flags & re.IGNORECASE] for a in atoms]}
    shown = " ".join(str(a) for a in atoms)
    try:
        r = subprocess.run([sys.executable, "-I", "-c", _REGEX_CHILD], input=json.dumps(job).encode("utf-8"),
                           capture_output=True, timeout=limit, check=False)
    except subprocess.TimeoutExpired:
        raise FilterError(f"{shown} took longer than {limit:g} s over {len(files)} files; narrow it with path: "
                          "or lang:, or write a pattern that backtracks less") from None
    except OSError as exc:
        raise FilterError(f"{shown} could not be searched: {exc}") from None
    if r.returncode != 0:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        raise FilterError(f"{shown} could not be searched: {tail[0]}")
    return json.loads(r.stdout.decode("ascii"))


class Selector:
    """Evaluates a :class:`Filters` expression on the index's units. Called as ``search_index.rank``'s ``where``:
    the units kept, each with its reasons and the line a regex or word atom matched (None when no line did)."""

    def __init__(self, filters: Filters, root: Path):
        self.f = filters
        self.root = Path(root)
        self._reason: dict[str, str | None] = {}
        self._vcache: dict = {}
        self._quals: dict[int, str] = {}
        self._rx: dict[int, dict[str, list[int]]] = {}   # id(atom) -> file -> matching lines
        self._paths = {a.value: repo_path(a.value, self.root) for a in filters.atoms if a.kind == "path"}

    def __call__(self, h, conn, uids: list[int]) -> dict[int, tuple[list[str], int | None]]:
        if self.f.expr is None:
            return {u: ([], None) for u in uids}
        if any(a.kind == "symbol" for a in self.f.atoms):
            want = [u for u in uids if h.units[u][2] == "symbol"]
            for k in range(0, len(want), 900):
                chunk = want[k:k + 900]
                self._quals.update(conn.execute(
                    f"SELECT uid, qual FROM units WHERE uid IN ({','.join('?' * len(chunk))})", chunk).fetchall())
        regexes = [a for a in self.f.atoms if a.kind == "regex"]
        if regexes:
            # only the files of units whose outcome the regexes can still change are searched
            files = sorted({h.units[u][0] for u in uids if self._decided(self.f.expr, h, u) is None})
            found = regex_lines(self.root, files, regexes) if files else [{} for _ in regexes]
            self._rx = {id(a): m for a, m in zip(regexes, found)}
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

    def _decided(self, e, h, u) -> bool | None:
        """``e`` for unit ``u`` with every regex atom unknown (three-valued): None when a regex decides it."""
        if isinstance(e, Atom):
            return None if e.kind == "regex" else self._atom(e, h, u)[0]
        op, arg = e
        if op == "not":
            x = self._decided(arg, h, u)
            return None if x is None else not x
        vals = [self._decided(x, h, u) for x in sorted(arg, key=_cost)]
        stop = op == "or"   # the value that settles an OR (True) or an AND (False)
        return stop if stop in vals else (None if None in vals else not stop)

    def _atom(self, a: Atom, h, u) -> tuple[bool, tuple[Atom, int] | None]:
        file, _nid, kind, name, lo, hi = h.units[u][:6]
        if a.kind == "path":
            rel = self._paths.get(a.value)
            if rel is not None:  # an absolute path: that file, or the files below that folder
                f = file.lower()
                return (not rel or f == rel or f.startswith(rel + "/")), None
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
        if a.kind == "regex":
            hits = self._rx.get(id(a), {}).get(file, [])
            k = bisect.bisect_left(hits, max(1, lo))
            return (True, (a, hits[k])) if k < len(hits) and hits[k] <= hi else (False, None)
        lines = file_lines(self.root / file) or []
        for n in range(max(1, lo), min(hi, len(lines)) + 1):
            if a.rx.search(lines[n - 1]):
                return True, (a, n)
        return False, None

    def _why(self, file: str) -> str | None:
        if file not in self._reason:
            self._reason[file] = vendored_reason(self.root / file, self.root, self._vcache)
        return self._reason[file]
