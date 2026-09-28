"""Minecraft datapacks: function calls, entity tags and scoreboard objectives (docs/DESIGN.md D52).

A mod with a datapack keeps half its behaviour in ``.mcfunction`` files: ``function ns:x`` and ``execute ... run
function ns:x`` call another function, ``schedule function ns:x 20t`` calls it later, ``#minecraft:tick`` runs a
function every tick. Entity tags (``tag @s add x``, ``@e[tag=x]``) and scoreboard objectives (``scoreboard players
set #m k 1``, ``execute if score #m k matches 0``) are state that mcfunction and Java share: Java adds and checks
the same tags (``addTag``, ``entityTags().contains``, often through a ``static final String`` constant) and reads
the same scores. A tag something checks but nothing adds, or a score something writes but nothing reads, is the
kind of bug neither side shows alone.

:func:`functions` finds the function files (``data/<ns>/function[s]/<path>.mcfunction`` outside build output);
:func:`parse_function` reads one; :func:`index` builds the cross-language index of tags and objectives;
:func:`problems` lists the mismatches. The Java that runs a function - a command string, the function manager's
lookup of an identifier, or the project's own helper around it (D69) - is read by :mod:`verinoda.datapack_java`, so
``datapack function ns:x`` lists Java callers beside mcfunction ones and a Java call to a function that does not
exist is a mismatch too. Read from the text as written: a Java tag constant is the one Java binds (its class's own,
``Owner.NAME``, a static import; D70), ``c ? A : B`` adds both, and a tag name Java builds at run time (``a.tag +
"_at"``) is a pattern (``*_at``) that marks a checked tag as maybe added, never as added; an objective name built at
run time or a macro ``$(name)`` is not seen, so a mismatch is a lead to check, not a proof; a function name built at
run time is listed as dynamic.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_SKIP = {"build", ".gradle", "out", "bin", "node_modules", ".git", ".verinoda", "run", ".idea", "graphify-out"}
_FUNC_DIRS = ("function", "functions")
_ID = r"#?[a-z0-9_.-]+:[a-z0-9_./-]+"
_CALL = re.compile(rf"(?:^|\s)function\s+({_ID})")
_SCHEDULE = re.compile(rf"(?:^|\s)schedule\s+function\s+({_ID})\s+(\d+[tsd]?)")
_TAG_CMD = re.compile(r"(?:^|\s)tag\s+(\S+(?:\[[^\]]*\])?)\s+(add|remove)\s+([A-Za-z0-9_.+-]+)")
_SEL_TAG = re.compile(r"\btag\s*=\s*(!?)([A-Za-z0-9_.+-]+)")
_NBT_TAGS = re.compile(r"\bTags\s*:\s*\[([^\]]*)\]")
_SEL_SCORES = re.compile(r"\bscores\s*=\s*\{([^}]*)\}")
_OBJ_ADD = re.compile(r"(?:^|\s)scoreboard\s+objectives\s+add\s+([A-Za-z0-9_.+-]+)")
_OBJ_REMOVE = re.compile(r"(?:^|\s)scoreboard\s+objectives\s+remove\s+([A-Za-z0-9_.+-]+)")
_PLAYERS = re.compile(r"(?:^|\s)scoreboard\s+players\s+(set|add|remove|reset|get|operation|enable|display)\s+"
                      r"(\S+)(?:\s+([A-Za-z0-9_.+-]+))?(?:\s+(\S+)\s+(\S+)(?:\s+([A-Za-z0-9_.+-]+))?)?")
_IF_SCORE = re.compile(r"\b(?:if|unless)\s+score\s+\S+\s+([A-Za-z0-9_.+-]+)(?:\s+(?:[<>=]+)\s+\S+\s+([A-Za-z0-9_.+-]+))?")
_STORE_SCORE = re.compile(r"\bstore\s+(?:result|success)\s+score\s+\S+\s+([A-Za-z0-9_.+-]+)")

# Java: tags through the entity API (the methods, or the live set `entityTags()` returns), objectives by name
_J_TAG_CALL = re.compile(r"\.(addTag|removeTag|addScoreboardTag|removeScoreboardTag)\s*\("
                         r"|\.(?:entityTags|getTags|getScoreboardTags|getCommandTags)\s*\(\s*\)\s*\.\s*(add|remove|contains)"
                         r"\s*\(")
_J_STR_CONST = re.compile(r"\bstatic\s+final\s+String\s+([A-Z][A-Z0-9_]*)\s*=\s*\"([^\"\\]*)\"\s*;")
_J_CONST_DECL = re.compile(r"\bstatic\s+final\s+String\s+([A-Z][A-Z0-9_]*)\s*=\s*([^;]+);")
_J_CLASS = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Z][\w$]*)[^{;]*\{")
_J_METHOD = re.compile(r"\b[\w<>\[\], ?]+\s+([a-z][\w$]*)\s*\(([^()]*)\)\s*(?:throws\s+[\w., ]+)?\{")
_J_STATIC_IMPORT = re.compile(r"^\s*import\s+static\s+(?:[\w$]+\.)*([A-Z][\w$]*)\.([A-Z][A-Z0-9_]*|\*)\s*;", re.M)
_J_CONST_NAME = re.compile(r"(?:[A-Za-z_$][\w$]*\.)*[A-Z][A-Z0-9_]*")
_J_STRING = re.compile(r"\"([A-Za-z0-9_.+-]{2,})\"")


@dataclass(frozen=True)
class Site:
    file: str
    line: int
    lang: str        # "mcfunction" or "java"
    kind: str        # tags: add / remove / check; objectives: define / write / read / use
    text: str

    @property
    def at(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class Function:
    id: str
    files: list[str] = field(default_factory=list)
    calls: list[tuple[int, str, str, str | None]] = field(default_factory=list)  # (line, target, how, delay)
    events: list[str] = field(default_factory=list)                              # #minecraft:tick ...


def _skipped(rel_parts) -> bool:
    return any(p in _SKIP for p in rel_parts)


def function_id(path: Path) -> str | None:
    """``.../data/<ns>/function[s]/<a/b>.mcfunction`` -> ``ns:a/b``."""
    parts = path.as_posix().split("/")
    for i in range(len(parts) - 3, -1, -1):
        if parts[i] == "data" and i + 2 < len(parts) and parts[i + 2] in _FUNC_DIRS:
            rest = "/".join(parts[i + 3:])
            return f"{parts[i + 1]}:{rest[:-len('.mcfunction')]}" if rest.endswith(".mcfunction") else None
    return None


def datapack_root(path: Path) -> Path | None:
    parts = list(path.parts)
    for i in range(len(parts) - 3, -1, -1):
        if parts[i] == "data" and i + 2 < len(parts) and parts[i + 2] in _FUNC_DIRS:
            return Path(*parts[:i])
    return None


def resolve_call(root: Path, fid: str) -> Path | None:
    """The file of function ``ns:path`` in the datapack at ``root`` (``function`` or the older ``functions``)."""
    if fid.startswith("#") or ":" not in fid:
        return None
    ns, _, p = fid.partition(":")
    for d in _FUNC_DIRS:
        cand = root / "data" / ns / d / f"{p}.mcfunction"
        if cand.is_file():
            return cand
    return None


def tag_members(root: Path, tag: str) -> list[str]:
    """The functions a function tag (``#minecraft:tick``) lists in the datapack at ``root``."""
    ns, _, p = tag.lstrip("#").partition(":")
    for d in ("function", "functions"):
        f = root / "data" / ns / "tags" / d / f"{p}.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        vals = data.get("values") if isinstance(data, dict) else None
        return [v.get("id") if isinstance(v, dict) else v for v in vals or [] if isinstance(v, (str, dict))]
    return []


def parse_function(text: str) -> tuple[list[tuple[int, str, str, str | None]], list[tuple[int, str, str]],
                                        list[tuple[int, str, str]]]:
    """``(calls, tag sites, objective sites)`` of one mcfunction's text: calls ``(line, target, how, delay)``, tag
    sites ``(line, name, add|remove|check)``, objective sites ``(line, name, define|write|read)``."""
    calls, tags, objs = [], [], []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("$"):  # a macro line: its $(names) are filled at run time
            line = line[1:]
        for m in _SCHEDULE.finditer(line):
            calls.append((i, m.group(1), "schedule", m.group(2)))
        sched = {m.start(1) for m in _SCHEDULE.finditer(line)}
        for m in _CALL.finditer(line):
            if m.start(1) in sched:
                continue
            how = "execute run" if re.search(r"(?:^|\s)execute\s", line[:m.start()]) else "function"
            calls.append((i, m.group(1), how, None))
        for m in _TAG_CMD.finditer(line):
            tags.append((i, m.group(3), m.group(2)))
        for m in _NBT_TAGS.finditer(line):  # summon ... {Tags:["a","b"]}, data merge entity ... {Tags:[...]}
            for name in re.findall(r'"?([A-Za-z0-9_.+-]+)"?', re.sub(r'\$\([^)]*\)', '', m.group(1))):
                tags.append((i, name, "add"))
        if "$(" in line and re.search(r"(?:Tags\s*:\s*\[[^\]]*\$\(|\s(?:add|remove)\s+\$\()", line):
            tags.append((i, "*", "add"))  # a macro fills in the tag
        for m in _SEL_TAG.finditer(line):
            if m.group(2):
                tags.append((i, m.group(2), "check"))
        for m in _SEL_SCORES.finditer(line):
            for part in m.group(1).split(","):
                name = part.split("=", 1)[0].strip()
                if name:
                    objs.append((i, name, "read"))
        for m in _OBJ_ADD.finditer(line):
            objs.append((i, m.group(1), "define"))
        for m in _OBJ_REMOVE.finditer(line):
            objs.append((i, m.group(1), "define"))
        for m in _PLAYERS.finditer(line):
            verb, obj = m.group(1), m.group(3)
            if obj:
                objs.append((i, obj, "read" if verb in ("get", "display") else "write"))
            if verb == "operation" and m.group(6):
                objs.append((i, m.group(6), "read"))
        for m in _IF_SCORE.finditer(line):
            objs.append((i, m.group(1), "read"))
            if m.group(2):
                objs.append((i, m.group(2), "read"))
        for m in _STORE_SCORE.finditer(line):
            objs.append((i, m.group(1), "write"))
    return calls, tags, objs


def functions(repo: Path) -> dict[str, Function]:
    """Every datapack function of the repository by id (a datapack copied into two places lists both files)."""
    repo = Path(repo)
    out: dict[str, Function] = {}
    from verinoda.snapshot import listed_files

    for p in [repo / r for r in listed_files(repo) if r.endswith(".mcfunction")]:
        rel = p.relative_to(repo)
        if _skipped(rel.parts[:-1]):
            continue
        fid = function_id(rel)
        if fid is None:
            continue
        fn = out.setdefault(fid, Function(fid))
        fn.files.append(rel.as_posix())
        if len(fn.files) == 1:
            try:
                fn.calls = parse_function(p.read_text(encoding="utf-8", errors="replace"))[0]
            except OSError:
                pass
            root = datapack_root(p)
            if root is not None:
                for ev in ("minecraft:tick", "minecraft:load"):
                    if fid in tag_members(root, ev):
                        fn.events.append("#" + ev)
    return out


_J_NOISE = re.compile(r"""//[^\n]*|/\*.*?(?:\*/|\Z)|\"\"\".*?(?:\"\"\"|\Z)|"(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.)*'?""",
                      re.S)
_BRACKETS = re.compile(r"[()\[\]{}]")
_NOT_NEWLINE = re.compile(r"[^\n]")
_NEWLINE = re.compile(r"\n")
_TAG_API = re.compile(r"\b(?:add|remove)(?:Scoreboard)?Tag\b|\b(?:entityTags|getTags|getScoreboardTags|getCommandTags)\b")


def _blank_match(m: re.Match) -> str:
    s = m.group(0)
    if s[:2] in ("//", "/*"):
        return _NOT_NEWLINE.sub(" ", s)
    q = '"""' if s.startswith('"""') else s[0]
    tail = q if len(s) >= 2 * len(q) and s.endswith(q) else ""
    return q + _NOT_NEWLINE.sub(" ", s[len(q):len(s) - len(tail)]) + tail


def _blank(text: str) -> str:
    """``text`` with the inside of its string and char literals and its comments replaced by spaces (newlines and
    offsets kept), so a brace, a parenthesis or a call spelled in a string is not code."""
    return _J_NOISE.sub(_blank_match, text)


def _close(text: str, open_pos: int) -> int:
    """The offset of the bracket closing the one at ``open_pos`` in a :func:`_blank`-ed text, or -1."""
    opener = text[open_pos]
    closer = {"(": ")", "[": "]", "{": "}"}[opener]
    depth = 0
    for m in _BRACKETS.finditer(text, open_pos):
        c = m.group()
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return m.start()
    return -1


def _top_level(expr: str, chars: str) -> list[int]:
    """Offsets of ``chars`` in ``expr`` outside brackets and literals (``::`` and ``++`` are not operators here)."""
    b, depth, out = _blank(expr), 0, []
    for i, ch in enumerate(b):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch in chars and b[i - 1:i + 1] != "::" and b[i:i + 2] not in ("::", "++") \
                and b[i - 1:i + 1] != "++":
            out.append(i)
    return out


def _split(expr: str, ch: str) -> list[str]:
    cuts = _top_level(expr, ch)
    return [expr[a + 1:b] for a, b in zip([-1] + cuts, cuts + [len(expr)])]


class _Consts:
    """The project's ``static final String`` constants by the class that declares them (D70): ``ETIKET`` in one
    class and in another are two constants, and a use reads the one Java binds - its own class (or an enclosing
    one), a class it names (``Croatoan.ETIKET``), a static import, another class of its file - before a name only
    one class declares. A constant may be built from others (``PREFIX + "_x"``)."""

    def __init__(self, texts: dict[str, str]):
        self.spans: dict[str, list[tuple[int, int, str]]] = {}         # file -> classes (start, end, name)
        self.decls: dict[tuple[str, int], dict[str, tuple[str, int]]] = {}  # class -> NAME -> (expr, offset)
        self.by_class: dict[str, list[tuple[str, int]]] = {}           # simple class name -> classes
        self.imports: dict[str, list[tuple[str, str]]] = {}            # file -> (owner, NAME or *)
        self.texts, self._blanked = texts, {}
        self._memo: dict[tuple[tuple[str, int], str], str | None] = {}
        for f, t in texts.items():  # the files that declare one; a file that only uses them is read when asked
            if _J_CONST_DECL.search(t):
                self.blank(f)

    def blank(self, f: str) -> str:
        """``f``'s :func:`_blank`-ed text; the first call reads its classes, constants and static imports."""
        if f not in self._blanked:
            t = self.texts[f]
            b = self._blanked[f] = _blank(t)
            spans = []
            for m in _J_CLASS.finditer(b):
                end = _close(b, m.end() - 1)
                spans.append((m.start(), end if end > 0 else len(b), m.group(1)))
                self.by_class.setdefault(m.group(1), []).append((f, m.start()))
            self.spans[f] = spans
            for m in _J_CONST_DECL.finditer(b):
                cls = self._innermost(f, m.start())
                if cls is not None:
                    self.decls.setdefault(cls, {})[m.group(1)] = (t[m.start(2):m.end(2)], m.start())
            self.imports[f] = [(m.group(1), m.group(2)) for m in _J_STATIC_IMPORT.finditer(b)]
        return self._blanked[f]

    def _enclosing(self, f: str, pos: int) -> list[tuple[str, int]]:
        """The classes of ``f`` around ``pos``, innermost first."""
        if f in self.texts:
            self.blank(f)
        around = [s for s in self.spans.get(f, []) if s[0] <= pos <= s[1]]
        return [(f, s[0]) for s in sorted(around, key=lambda s: s[0], reverse=True)]

    def _innermost(self, f: str, pos: int) -> tuple[str, int] | None:
        around = self._enclosing(f, pos)
        return around[0] if around else None

    def _in(self, cls: tuple[str, int], name: str, depth: int) -> str | None:
        if (cls, name) not in self._memo:
            self._memo[(cls, name)] = None  # a constant built from itself stays unknown
            expr, at = self.decls[cls][name]
            vals, pats = _values(expr, lambda e, d: self.value(e, cls[0], at, d), depth + 1)
            self._memo[(cls, name)] = next(iter(vals)) if len(vals) == 1 and not pats else None
        return self._memo[(cls, name)]

    def _of(self, classes, name: str, depth: int) -> str | None:
        """The value ``name`` has in ``classes`` when they agree on one."""
        vals = {self._in(c, name, depth) for c in classes if name in self.decls.get(c, {})}
        vals.discard(None)
        return next(iter(vals)) if len(vals) == 1 else None

    def value(self, expr: str, f: str, pos: int, depth: int = 0) -> str | None:
        """The value of the constant ``expr`` (``NAME``, ``Owner.NAME``, ``pkg.Owner.NAME``) used in ``f`` at
        ``pos``, or None."""
        if depth > 8 or not _J_CONST_NAME.fullmatch(expr):
            return None
        *owner, name = expr.split(".")
        if owner and owner[-1] != "this":
            v = self._of(self.by_class.get(owner[-1], []), name, depth)
            if v is not None:
                return v
        else:
            for cls in self._enclosing(f, pos):
                if name in self.decls.get(cls, {}):
                    return self._in(cls, name, depth)
            for own, what in self.imports.get(f, []):
                if what in (name, "*"):
                    v = self._of(self.by_class.get(own, []), name, depth)
                    if v is not None:
                        return v
            v = self._of([(f, s[0]) for s in self.spans.get(f, [])], name, depth)
            if v is not None:
                return v
        return self._of(list(self.decls), name, depth)

    def unique(self) -> dict[str, str]:
        """``NAME`` -> value for every name whose declarations that can be read agree on one value (the table
        :mod:`verinoda.datapack_java` looks names up in)."""
        names = {n for d in self.decls.values() for n in d}
        out = {n: self._of(list(self.decls), n, 0) for n in names}
        return {k: v for k, v in out.items() if v is not None}


def _unquote(lit: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), lit)


def _values(expr: str, const, depth: int = 0) -> tuple[set[str], list[str]]:
    """What a Java String expression can be: ``(names, patterns)``. A literal or a constant (``const(expr, depth)``)
    is one name; ``c ? A : B`` is both; a concatenation of known parts is a name, and with an unknown part a pattern
    (``a.tag + "_at"`` -> ``*_at``); anything else is the pattern ``*``."""
    e = expr.strip()
    while e.startswith("(") and _close(_blank(e), 0) == len(e) - 1:
        e = e[1:-1].strip()
    if not e or depth > 8:
        return set(), ["*"]
    q = _top_level(e, "?")
    if q:
        colons = [c for c in _top_level(e, ":") if c > q[0]]
        # the colon of this conditional: the first one after as many nested conditionals as the branch opens
        nested = [p for p in q[1:] if p < (colons[0] if colons else len(e))]
        if len(colons) > len(nested):
            c = colons[len(nested)]
            a, b = _values(e[q[0] + 1:c], const, depth + 1), _values(e[c + 1:], const, depth + 1)
            return a[0] | b[0], a[1] + b[1]
        return set(), ["*"]
    parts = _split(e, "+")
    if len(parts) > 1:
        options, pattern, exact = [""], "", True
        for p in parts:
            vals, pats = _values(p, const, depth + 1)
            if len(vals) == 1 and not pats:
                v = next(iter(vals))
                options = [o + v for o in options]
                pattern += v.replace("*", "")
            elif vals and not pats and len(options) * len(vals) <= 16:
                options = [o + v for o in options for v in sorted(vals)]
                pattern += "*"
            else:
                exact = False
                pattern += "*"
        return (set(options), []) if exact else (set(), [re.sub(r"\*+", "*", pattern)])
    m = re.fullmatch(r"\"((?:[^\"\\]|\\.)*)\"", e)
    if m:
        return {_unquote(m.group(1))}, []
    v = const(e, depth) if _J_CONST_NAME.fullmatch(e) else None
    return ({v}, []) if v is not None else (set(), ["*"])


def _pattern_rx(pattern: str) -> re.Pattern:
    return re.compile(".*".join(re.escape(p) for p in pattern.split("*")))


_READ_HINT = re.compile(r"getPlayerScoreInfo|getScore\b|\.value\(\)|getOrCreatePlayerScore\([^)]*\)\s*\.\s*get\b"
                        r"|\bget\(\)")
_WRITE_HINT = re.compile(r"\.set\(|setScore|\.add\(|increment|resetSinglePlayerScore|resetAllPlayerScores")


def index(repo: Path, *, java_files: list[str] | None = None, java_calls: bool = True) -> dict:
    """``{"functions", "tags": {name: [Site]}, "objectives": {name: [Site]}, "java": datapack_java.Result}`` over
    the datapacks and the Java sources (``java_files``, default every ``.java`` outside build output); ``java`` is
    None when ``java_calls`` is False."""
    repo = Path(repo)
    funcs = functions(repo)
    tags: dict[str, list[Site]] = {}
    objs: dict[str, list[Site]] = {}
    for fn in funcs.values():
        f = fn.files[0]
        try:
            lines = (repo / f).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        _c, ts, os_ = parse_function("\n".join(lines))
        for ln, name, kind in ts:
            tags.setdefault(name, []).append(Site(f, ln, "mcfunction", kind, lines[ln - 1].strip()[:160]))
        for ln, name, kind in os_:
            objs.setdefault(name, []).append(Site(f, ln, "mcfunction", kind, lines[ln - 1].strip()[:160]))
    if java_files is None:
        from verinoda.snapshot import listed_files

        java_files = [f for f in listed_files(repo) if f.endswith(".java") and not _skipped(f.split("/")[:-1])]
    texts: dict[str, str] = {}
    for f in java_files:
        try:
            texts[f] = (repo / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    cx = _Consts(texts)
    java = None
    if java_calls:
        from verinoda import datapack_java

        java = datapack_java.scan(repo, texts, consts=cx.unique())
    known_objs = set(objs)
    helpers = _tag_helpers(texts)
    patterns: list[tuple[str, Site]] = []   # tag names Java builds at run time: (pattern, the add)
    helper_rx = re.compile(rf"(?<![\w$.])({'|'.join(map(re.escape, sorted(helpers)))})\s*\(") if helpers else None
    for f, t in texts.items():
        lines = t.splitlines()
        starts = [0] + [m.end() for m in _NEWLINE.finditer(t)]

        def line_of(pos: int) -> int:
            import bisect
            return bisect.bisect_right(starts, pos)

        tagged = _J_TAG_CALL.search(t) or (helper_rx is not None and helper_rx.search(t))
        b = cx.blank(f) if tagged else ""
        methods = [(m.start(), {p.split()[-1] for p in m.group(2).split(",") if "String" in p and p.split()})
                   for m in _J_METHOD.finditer(b)]

        def tag_site(pos: int, expr: str, kind: str) -> None:
            ln = line_of(pos)
            site = Site(f, ln, "java", kind, lines[ln - 1].strip()[:160])
            names, pats = _values(expr, lambda e, d: cx.value(e, f, pos, d))
            for name in sorted(names):
                tags.setdefault(name, []).append(site)
            if kind != "add":
                return
            for p in dict.fromkeys(pats):
                if p == "*" and re.fullmatch(r"[a-z_$][\w$]*", expr.strip()):
                    own = next((ps for at, ps in reversed(methods) if at < pos), set())
                    if expr.strip() in own:  # a method handing its own parameter on: its callers name the tag
                        continue
                patterns.append((p, site))

        for m in _J_TAG_CALL.finditer(b):
            end = _close(b, m.end() - 1)
            if end < 0:
                continue
            verb = m.group(1) or m.group(2)
            kind = "check" if verb == "contains" else "add" if verb.startswith("add") else "remove"
            tag_site(m.start(), t[m.end():end], kind)
        if helper_rx is not None:  # the project's own tag helpers: `tag(e, "x")`, `etiketle(e, HURDA_ETIKET)`
            for m in helper_rx.finditer(b):
                h = helpers.get(m.group(1))
                end = _close(b, m.end() - 1) if h else -1
                if end < 0 or re.match(r"\s*(?:\{|throws\b)", b[end + 1:end + 40]):  # the declaration itself
                    continue
                args = _split(t[m.end():end], ",")
                if h[1] < len(args):
                    tag_site(m.start(), args[h[1]], h[0])
        # commands written as strings in Java (run through the server's command dispatcher)
        for m in re.finditer(r'"((?:[^"\\\n]|\\.){6,})"', t):
            s = m.group(1).replace('\\"', '"')
            if not re.search(r"(?:^|\s)(?:tag|scoreboard|summon|execute|function|data)\s", s):
                continue
            ln = line_of(m.start())
            _c, ts, os_ = parse_function(s)
            for _l, name, kind in ts:
                tags.setdefault(name, []).append(Site(f, ln, "java", kind, lines[ln - 1].strip()[:160]))
            for _l, name, kind in os_:
                objs.setdefault(name, []).append(Site(f, ln, "java", kind, lines[ln - 1].strip()[:160]))
        if not known_objs:
            continue
        # an objective named in a call: `helper(server, "k_durum")`, `sb.getObjective("k")`; read or write by what
        # the called method (in this file) or the enclosing statement does with it
        for m in _J_STRING.finditer(t):
            name = m.group(1)
            if name not in known_objs:
                continue
            ln = line_of(m.start())
            stmt = lines[ln - 1]
            call = re.search(r"([A-Za-z_$][\w$]*)\s*\([^()]*$", t[max(0, m.start() - 200):m.start()])
            kind = "use"
            if call:
                body = _method_body(t, call.group(1))
                probe = body if body is not None else stmt
                if _WRITE_HINT.search(probe):
                    kind = "write"
                elif _READ_HINT.search(probe):
                    kind = "read"
            objs.setdefault(name, []).append(Site(f, ln, "java", kind, stmt.strip()[:160]))
    return {"functions": funcs, "tags": tags, "objectives": objs, "java": java, "tag_patterns": patterns}


def _tag_helpers(texts: dict[str, str]) -> dict[str, tuple[str, int]]:
    """Methods of the project that add, remove or check an entity tag they are given: name -> (kind, index of the
    tag parameter); a name two methods use differently is dropped."""
    out: dict[str, set[tuple[str, int]]] = {}
    decl = re.compile(r"\b(?:static\s+)?(?:boolean|void|[A-Z][\w<>]*)\s+([a-z][\w$]*)\s*\(([^)]*String[^)]*)\)\s*\{")
    for text in texts.values():
        if not _TAG_API.search(text):  # a helper's body calls the tag API
            continue
        for m in decl.finditer(text):
            body = _method_body(text, m.group(1)) or ""
            if len(body) > 1500:
                continue
            params = [(k, p.split()[-1]) for k, p in enumerate(m.group(2).split(",")) if "String" in p and p.split()]
            live = r"(?:entityTags|getTags|getScoreboardTags|getCommandTags)\s*\(\s*\)\s*\.\s*"
            for k, prm in params:
                arg = rf"\s*\(\s*{re.escape(prm)}\s*\)"
                if re.search(rf"\.(?:addTag|addScoreboardTag){arg}|{live}add{arg}", body):
                    out.setdefault(m.group(1), set()).add(("add", k))
                elif re.search(rf"\.(?:removeTag|removeScoreboardTag){arg}|{live}remove{arg}", body):
                    out.setdefault(m.group(1), set()).add(("remove", k))
                elif re.search(rf"{live}contains{arg}", body):
                    out.setdefault(m.group(1), set()).add(("check", k))
    return {k: next(iter(v)) for k, v in out.items() if len(v) == 1}


def _method_body(text: str, name: str) -> str | None:
    """The body of method ``name`` declared in ``text`` (brace-matched), or None."""
    m = re.search(rf"\b[\w<>\[\], ]+\s+{re.escape(name)}\s*\([^)]*\)\s*(?:throws [\w., ]+)?\{{", text)
    if not m:
        return None
    depth, i = 0, m.end() - 1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[m.end():i]
        i += 1
    return None


def problems(ix: dict) -> dict:
    """Tags something checks that nothing adds, tags added that nothing checks, objectives written that nothing
    reads (a Java ``use`` of an objective counts as a possible read), and calls to functions that do not exist:
    from mcfunction, and from Java into a namespace the datapacks define (``getFunctions().get`` of a missing
    function returns nothing and the call does nothing, silently)."""
    out: dict[str, list] = {"tags_checked_never_added": [], "tags_added_never_checked": [],
                            "scores_written_never_read": [], "missing_functions": []}
    macro = [s.at for s in ix["tags"].get("*", [])]
    if macro:  # tags a macro fills in cannot be matched: said, not guessed
        out["tags_added_by_macros"] = macro[:10]
    built = [{"at": s.at, "pattern": p} for p, s in ix.get("tag_patterns") or []]
    if built:  # tag names Java builds at run time: matched as patterns, never as a name
        out["tags_added_dynamically"] = built
    for name, sites in sorted(ix["tags"].items()):
        if name == "*":
            continue
        kinds = {s.kind for s in sites}
        if "check" in kinds and "add" not in kinds:
            row = {"name": name, "sites": [s.at for s in sites if s.kind == "check"]}
            maybe = _maybe_added_by(ix, name)
            if maybe:  # no add spells it, but a name built at run time may be it: a lead, not "never"
                row["maybe_added_by"] = maybe
            out["tags_checked_never_added"].append(row)
        elif "add" in kinds and "check" not in kinds:
            out["tags_added_never_checked"].append({"name": name, "sites": [s.at for s in sites if s.kind == "add"]})
    for name, sites in sorted(ix["objectives"].items()):
        kinds = {s.kind for s in sites}
        if "write" in kinds and not kinds & {"read", "use"}:
            out["scores_written_never_read"].append({"name": name,
                                                     "sites": [s.at for s in sites if s.kind == "write"][:10]})
    funcs = ix["functions"]
    for fid, fn in sorted(funcs.items()):
        for ln, target, how, _d in fn.calls:
            if not target.startswith("#") and target not in funcs:
                out["missing_functions"].append({"name": target, "called_at": f"{fn.files[0]}:{ln}", "how": how})
    spaces = {fid.split(":", 1)[0] for fid in funcs}
    for c in (ix.get("java").calls if ix.get("java") else []):
        if not c.dynamic and not c.target.startswith("#") and c.target not in funcs \
                and c.target.split(":", 1)[0] in spaces:
            out["missing_functions"].append({"name": c.target, "called_at": c.at, "how": f"java {c.via}",
                                             "caller": c.caller, **({"helper": c.helper} if c.helper else {}),
                                             **({"tree": c.tree} if c.tree else {})})
    return out


def _maybe_added_by(ix: dict, name: str) -> list[dict]:
    """The Java adds whose name, built at run time, fits ``name`` (``*_at`` fits ``olum_at``); a name nothing about
    is known (``*``) fits every tag and is listed in the summary only."""
    return [{"at": s.at, "pattern": p} for p, s in ix.get("tag_patterns") or []
            if p != "*" and _pattern_rx(p).fullmatch(name)]


def _called_by(funcs: dict[str, Function], fid: str) -> list[tuple[str, int, str, str | None]]:
    return [(f.id, ln, how, d) for f in funcs.values() for ln, t, how, d in f.calls if t == fid]


def _java_rows(ix: dict, fid: str) -> tuple[list[dict], list[dict], list[dict]]:
    """The Java calls of ``fid`` (own tree first); the names built at run time that may be it (a known part past
    the namespace: ``ns:prefix_*``); and the sites that build all of the name past the namespace (``ns:*``) or the
    whole name, which fit every function and are listed in full in the summary only."""
    java = ix.get("java")
    if java is None:
        return [], [], []
    from verinoda.testcode import is_test_file

    calls = [c.record() for c in sorted((c for c in java.calls if not c.dynamic and c.target == fid),
                                        key=lambda c: (c.tree is not None, is_test_file(c.file), c.file, c.line))]
    maybe = [c.record() for c in java.calls if c.dynamic and ":" in c.target and not c.target.endswith(":")
             and c.matches(fid)]
    bare = [c.record() for c in sorted((c for c in java.calls if c.dynamic and (not c.target or (
        c.target.endswith(":") and c.matches(fid)))), key=lambda c: (c.tree is not None, is_test_file(c.file),
                                                                     c.file, c.line))]
    return calls, maybe, bare


def _java_summary(java) -> dict:
    from verinoda.testcode import is_test_file

    by_via: dict[str, int] = {}
    for c in java.calls:
        if not c.dynamic:
            by_via[c.via] = by_via.get(c.via, 0) + 1
    bound = [c for c in java.calls if not c.dynamic]
    helpers = sorted(java.helpers, key=lambda h: (h.tree is not None, is_test_file(h.file), h.source != "body",
                                                  h.file, h.line))
    return {"calls": len(bound), "by_via": by_via,
            "in_tests": len([c for c in bound if not c.tree and is_test_file(c.file)]),
            "in_reference_trees": len([c for c in bound if c.tree]),
            "helpers": [h.record() for h in helpers],
            "dynamic": [c.record() for c in java.calls if c.dynamic],
            "unresolved_lookups": java.unresolved}


def lookup(repo: Path, what: str | None = None, name: str | None = None) -> dict:
    """``verinoda datapack``: no argument -> the functions, tags and objectives counted and the mismatches;
    ``tag NAME`` / ``score NAME`` -> every site; ``function ID`` -> what it calls and what calls it."""
    ix = index(repo, java_calls=what in (None, "", "function"))
    base = {"functions": len(ix["functions"]), "tags": len([t for t in ix["tags"] if t != "*"]),
            "objectives": len(ix["objectives"])}
    if ix.get("java") is not None:
        base["java_calls"] = len([c for c in ix["java"].calls if not c.dynamic])
    if not what:
        if not ix["functions"] and not ix["tags"]:
            return {**base, "status": "no_datapack", "note": "no data/<namespace>/function[s]/*.mcfunction and no "
                                                            "entity tags in Java"}
        return {**base, "status": "found", "kind": "summary", "problems": problems(ix),
                **({"java": _java_summary(ix["java"])} if ix.get("java") is not None else {}),
                "note": "read from the text: a name built at run time is not a name (a function's is listed as "
                        "dynamic, a Java tag's matched as a pattern); a mismatch is a lead"}
    if what in ("tag", "score", "objective"):
        table = ix["tags"] if what == "tag" else ix["objectives"]
        sites = table.get(name or "")
        maybe = _maybe_added_by(ix, name or "") if what == "tag" else []
        if not sites:
            from difflib import get_close_matches

            return {**base, "status": "not_found", "kind": what, "name": name,
                    "nearest": get_close_matches(name or "", [k for k in table if k != "*"], n=5),
                    **({"maybe_added_by": maybe} if maybe else {})}
        return {**base, "status": "found", "kind": what, "name": name,
                "sites": [{"at": s.at, "lang": s.lang, "kind": s.kind, "text": s.text} for s in sites],
                **({"maybe_added_by": maybe} if maybe else {})}
    if what == "function":
        fn = ix["functions"].get(name or "")
        java_calls, maybe, bare = _java_rows(ix, name or "")
        if fn is None:
            from difflib import get_close_matches

            return {**base, "status": "not_found", "kind": "function", "name": name,
                    "nearest": get_close_matches(name or "", list(ix["functions"]), n=5),
                    **({"called_by": java_calls} if java_calls else {}), **({"dynamic": maybe} if maybe else {})}
        return {**base, "status": "found", "kind": "function", "name": fn.id, "files": fn.files, "events": fn.events,
                "calls": [{"line": ln, "target": t, "how": how, **({"delay": d} if d else {})}
                          for ln, t, how, d in fn.calls],
                "called_by": [{"kind": "mcfunction", "function": f, "at": f"{ix['functions'][f].files[0]}:{ln}",
                               "how": how, **({"delay": d} if d else {})}
                              for f, ln, how, d in _called_by(ix["functions"], fn.id)] + java_calls,
                "dynamic": maybe, "dynamic_any": bare}
    raise ValueError(f"datapack: unknown lookup {what!r} (tag, score, function)")


def _java_line(c: dict) -> str:
    """``called by Java Weapon.fire at src/Weapon.java:42 (helper Mod.runFunction)``."""
    via = {"helper": f"helper {c.get('helper')}", "identifier": "identifier",
           "command-string": "command string" + (f", {c['how']}" if c.get("how") not in (None, "function") else "")}
    tree = f" [reference tree {c['tree']}]" if c.get("tree") else ""
    test = " [test]" if c.get("test") else ""
    return f"called by Java {c['caller']} at {c['at']} ({via.get(c['via'], c['via'])}){test}{tree}"


def _dynamic_line(c: dict) -> str:
    what = f"builds {c['target']}" if c["target"] != "*" else "builds the whole name at run time"
    tree = f" [reference tree {c['tree']}]" if c.get("tree") else ""
    how = f"helper {c['helper']}" if c.get("helper") else c["via"].replace("-", " ")
    return f"dynamic: {c['at']} {what} (Java {c['caller']}, {how}){tree}"


def render(res: dict) -> str:
    head = f"{res['functions']} function(s), {res['tags']} entity tag(s), {res['objectives']} objective(s)"
    if "java_calls" in res:
        head += f", {res['java_calls']} Java call(s) into the functions"
        js = res.get("java")
        if js and (js.get("in_tests") or js.get("in_reference_trees")):
            head += f" ({js['calls'] - js['in_tests'] - js['in_reference_trees']} in product code)"
    if res["status"] == "no_datapack":
        return f"{head}: {res['note']}"
    if res["status"] == "not_found":
        near = ", ".join(res.get("nearest") or [])
        out = [f"{res['kind']} {res['name']}: not found" + (f" (nearest: {near})" if near else "")]
        if res.get("called_by"):  # Java runs a function the datapacks do not have: the lookup finds nothing
            out.append(f"  Java calls it, and the datapacks have no {res['name']}:")
            out += ["  " + _java_line(c) for c in res["called_by"]]
        out += ["  " + _dynamic_line(c) + " - may be this one" for c in res.get("dynamic") or []]
        out += [f"  dynamic: {_built_add(d)} - may be this one" for d in res.get("maybe_added_by") or []]
        return "\n".join(out)
    if res["kind"] == "summary":
        out = [head]
        pr = res["problems"]
        labels = {"tags_checked_never_added": "tags checked but never added",
                  "tags_added_never_checked": "tags added but never checked",
                  "scores_written_never_read": "objectives written but never read",
                  "missing_functions": "calls to functions that do not exist"}
        for key, label in labels.items():
            rows = pr.get(key) or []
            out.append(f"{label} ({len(rows)}):")
            for r in rows[:15]:
                where = r.get("called_at") or ", ".join(r.get("sites", [])[:2])
                java = f" (Java {r['caller']}, {r['how'][5:].replace('-', ' ')})" if r.get("caller") else ""
                tree = f" [reference tree {r['tree']}]" if r.get("tree") else ""
                out.append(f"  {r['name']}  {where}{java}{tree}{_maybe_note(r.get('maybe_added_by'))}")
            if len(rows) > 15:
                out.append(f"  (+{len(rows) - 15} more: --json)")
        if pr.get("tags_added_by_macros"):
            out.append("tags a macro fills in (not matched): " + ", ".join(pr["tags_added_by_macros"][:3]))
        built = pr.get("tags_added_dynamically") or []
        if built:
            out.append(f"tag names Java builds at run time ({len(built)}): "
                       + ", ".join(_built_add(d) for d in built[:3]) + (" ..." if len(built) > 3 else ""))
        java = res.get("java")
        if java:
            by = ", ".join(f"{k.replace('-', ' ')} {v}" for k, v in sorted(java["by_via"].items()))
            where = [f"{java[k]} {w}" for k, w in (("in_tests", "in tests"), ("in_reference_trees",
                                                                                "in reference trees")) if java.get(k)]
            out.append(f"Java calls into the functions ({java['calls']}): {by or 'none'}"
                       + (f"; {', '.join(where)}" if where else ""))
            if java["helpers"]:
                hs = [f"{h['helper']} ({h['at'].rsplit('/', 1)[-1]})" if h.get("at") else f"{h['helper']} (config)"
                      for h in java["helpers"][:8]]
                more = f" (+{len(java['helpers']) - 8} more: --json)" if len(java["helpers"]) > 8 else ""
                out.append(f"  helpers recognised ({len(java['helpers'])}): " + ", ".join(hs) + more)
            dyn = java["dynamic"]
            if dyn:
                out.append(f"function names Java builds at run time ({len(dyn)}, not bound to a function):")
                out += ["  " + _dynamic_line(c) for c in dyn[:15]]
                if len(dyn) > 15:
                    out.append(f"  (+{len(dyn) - 15} more: --json)")
            if java.get("unresolved_lookups"):
                un = java["unresolved_lookups"]
                out.append(f"function lookups whose id the text does not spell ({len(un)}): " + ", ".join(un[:5])
                           + (" ..." if len(un) > 5 else ""))
        out.append(f"note: {res['note']}")
        return "\n".join(out)
    if res["kind"] == "function":
        out = [f"{res['name']} ({', '.join(res['files'])})" + (f" - runs on {', '.join(res['events'])}"
                                                               if res["events"] else "")]
        out += [f"  calls {c['target']} ({c['how']}{' ' + c['delay'] if c.get('delay') else ''}) at line {c['line']}"
                for c in res["calls"]]
        out += [f"  called by {c['function']} at {c['at']} ({c['how']}{' ' + c['delay'] if c.get('delay') else ''})"
                if c.get("kind") != "java" else "  " + _java_line(c) for c in res["called_by"]]
        out += ["  " + _dynamic_line(c) + " - may be this one" for c in res.get("dynamic") or []]
        bare = res.get("dynamic_any") or []
        if bare:  # Java builds the whole name at run time (a table, a command argument): it may run this one
            shown = ", ".join(c["at"] + (" [test]" if c.get("test") else "") +
                              (f" [reference tree {c['tree']}]" if c.get("tree") else "") for c in bare[:5])
            out.append(f"  {len(bare)} Java site(s) build the name at run time and may run this one too (listed in "
                       f"the summary): {shown}" + (" ..." if len(bare) > 5 else ""))
        if not res["calls"] and not res["called_by"] and not res["events"] and not res.get("dynamic") and not bare:
            out.append("  no call in or out found in the datapacks or in Java")
        return "\n".join(out)
    out = [f"{res['kind']} {res['name']}: {len(res['sites'])} site(s)"]
    out += [f"  {s['kind']:<6} {s['lang']:<10} {s['at']}: {s['text']}" for s in res["sites"][:40]]
    out += [f"  maybe  java       {_built_add(d)} (a name built at run time)" for d in res.get("maybe_added_by") or []]
    return "\n".join(out)


def _built_add(d: dict) -> str:
    return f"{d['at']} adds " + (d["pattern"] if d["pattern"] != "*" else "a name the text does not spell")


def _maybe_note(maybe: list[dict] | None) -> str:
    """``  (maybe added by Atlilar.java:751: *_at)`` after a tag checked but never added by name."""
    if not maybe:
        return ""
    shown = ", ".join(f"{d['at']}: {d['pattern']}" for d in maybe[:2])
    return f"  (maybe added by {shown}" + (f" +{len(maybe) - 2} more" if len(maybe) > 2 else "") + ")"
