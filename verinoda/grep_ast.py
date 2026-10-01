"""Structural search: code-shaped patterns with metavariables over the tree-sitter trees of the project's files.

``verinoda grep-ast 'foo($A, $$$REST)'`` parses the pattern with the grammar of each language it searches (the
grammars the index already reads, plus Python's), then walks every file of that language and reports each node
whose kind and children match the pattern's:

- ``$A`` (an upper-case name) matches one named node and captures its text; the same name twice must capture the
  same text; ``$_`` matches one node without capturing;
- ``$$$REST`` matches zero or more nodes in a sequence of children (``$$$`` alone: without capturing);
- anything else must be the same node kind with the same children; a leaf must have the same text. Commas and
  semicolons between children are not compared (so ``foo($A, $$$REST)`` matches ``foo(1)``) and comments in
  the code are skipped.

A pattern that is not a whole construct of a language on its own (a Java call has no top level) is parsed inside
the smallest wrapper that makes it one (a statement, a class body, a method body); the pattern's node is the
innermost one that spans exactly its text. A language whose grammar cannot parse the pattern is not searched and
is named. Rule files (``--rule FILE``) hold one rule per YAML document (``id``, ``language``, ``pattern`` or
``rule: {pattern: ...}``, ``message``).

Each match is ``statically_verified``: the matched text is in the file, at that line, as it is on disk now (the
files are read directly, not the index, so nothing is stale). A match is about the shape of the code only: names
are not resolved, so ``foo(...)`` also matches a different ``foo``. Each file has a time bound (a file past it is
named, not guessed about) and the whole search one more; files over 1 MB are skipped and counted. Read-only.
"""
from __future__ import annotations

import re
import time
from functools import lru_cache
from pathlib import Path

MAX_RESULTS = 200
MAX_BYTES = 1_000_000
FILE_SECONDS = 2.0
TOTAL_SECONDS = 60.0
_SHOWN = 200          # characters of a matched text or a capture

_PY = {".py": ("tree_sitter_python", "language"), ".pyi": ("tree_sitter_python", "language")}
# language names for --lang and the output, by grammar
_LANG_NAMES = {
    ("tree_sitter_python", "language"): "python", ("tree_sitter_javascript", "language"): "javascript",
    ("tree_sitter_typescript", "language_typescript"): "typescript",
    ("tree_sitter_typescript", "language_tsx"): "tsx", ("tree_sitter_go", "language"): "go",
    ("tree_sitter_java", "language"): "java", ("tree_sitter_rust", "language"): "rust",
    ("tree_sitter_c", "language"): "c", ("tree_sitter_cpp", "language"): "cpp",
    ("tree_sitter_ruby", "language"): "ruby", ("tree_sitter_c_sharp", "language"): "csharp",
    ("tree_sitter_php", "language_php"): "php", ("tree_sitter_kotlin", "language"): "kotlin",
    ("tree_sitter_scala", "language"): "scala", ("tree_sitter_swift", "language"): "swift",
    ("tree_sitter_lua", "language"): "lua",
}
_ALIASES = {"py": "python", "js": "javascript", "ts": "typescript", "c#": "csharp", "cs": "csharp",
            "c++": "cpp", "kt": "kotlin", "rb": "ruby"}

_MULTI = re.compile(r"\$\$\$([A-Z_][A-Z0-9_]*)?")
_SINGLE = re.compile(r"\$([A-Z_][A-Z0-9_]*)")
_MV_TEXT = re.compile(r"^VNMV(S?)_([A-Z0-9_]*)$")
_SEPARATORS = {",", ";"}
# wrappers that make a fragment a whole program, tried in order (the first that parses without an error wins)
_WRAPS = ("{p}", "{p};", "class W_ {{ {p} }}", "class W_ {{ void m_() {{ {p} }} }}",
          "class W_ {{ void m_() {{ {p}; }} }}", "class W_ {{ Object f_ = {p}; }}", "void m_() {{ {p}; }}",
          "fn m_() {{ {p}; }}", "package w_\nfunc m_() {{ {p} }}", "fun m_() {{ {p} }}", "func m_() {{ {p} }}")


class PatternError(ValueError):
    """The pattern parses in none of the languages asked for, or is only a metavariable."""


class _Timeout(Exception):
    pass


def languages() -> dict[str, tuple[str, str]]:
    """Suffix -> grammar (module, function) of every language structural search reads."""
    from verinoda.anchors import TS_LANGS

    return {**TS_LANGS, **_PY}


def lang_name(suffix: str) -> str | None:
    spec = languages().get(suffix)
    return _LANG_NAMES.get(spec) if spec else None


def known_langs() -> list[str]:
    return sorted(set(_LANG_NAMES.values()))


def norm_lang(name: str) -> str:
    n = name.strip().lower()
    return _ALIASES.get(n, n)


@lru_cache(maxsize=4)
def _py_parser():
    try:
        import tree_sitter_python
        from tree_sitter import Language, Parser

        return Parser(Language(tree_sitter_python.language()))
    except Exception:  # noqa: BLE001 - grammar missing or incompatible: Python is not searched (said)
        return None


def _parser(suffix: str):
    if suffix in _PY:
        return _py_parser()
    from verinoda.anchors import _ts_parser

    return _ts_parser(suffix)


# -- the pattern -----------------------------------------------------------------------------------------------

def _placeholders(pattern: str) -> str:
    text = _MULTI.sub(lambda m: f"VNMVS_{m.group(1) or ''}", pattern)
    return _SINGLE.sub(lambda m: f"VNMV_{m.group(1)}", text)


def _mv(node) -> tuple[bool, str] | None:
    """(is_multi, name) when the pattern node is a metavariable (or a node that only wraps one), else None."""
    while True:
        m = _MV_TEXT.match(node.text.decode("utf-8", "replace").rstrip(";").strip())
        if m is None:
            return None
        if node.child_count == 0 or node.named_child_count == 0:
            return bool(m.group(1)), m.group(2)
        named = [c for c in node.children if c.is_named]
        if len(named) != 1:
            return None
        node = named[0]


@lru_cache(maxsize=256)
def _compile(pattern: str, suffix: str):
    """The pattern's node in the grammar of ``suffix`` (None when no wrapper parses it without an error)."""
    parser = _parser(suffix)
    if parser is None:
        return None
    body = _placeholders(pattern.strip())
    for wrap in _WRAPS:
        text = wrap.format(p=body)
        tree = parser.parse(text.encode("utf-8"))
        if tree.root_node.has_error:
            continue
        start = len(text[:text.index(body)].encode("utf-8"))
        end = start + len(body.encode("utf-8"))
        # the innermost node with exactly the pattern's span; else with the span plus a `;` the wrapper added
        # (a Java `return x` is a statement only with it)
        ends = (end, end + 1) if text.encode("utf-8")[end:end + 1] == b";" else (end,)
        for e in ends:
            best, stack = None, [tree.root_node]
            while stack:
                n = stack.pop()
                if n.start_byte == start and n.end_byte == e:
                    best = n   # deeper nodes come later
                if n.start_byte <= start and n.end_byte >= e:
                    stack.extend(n.children)
            if best is not None:
                return best
    return None


def _needles(pnode) -> frozenset[bytes]:
    """The texts of the pattern's leaves outside its metavariables: a file that lacks one cannot match (it is not
    parsed)."""
    out, stack = set(), [pnode]
    while stack:
        n = stack.pop()
        if _mv(n) is not None or "comment" in n.type:
            continue
        if n.child_count == 0:
            if n.text and n.text.decode("utf-8", "replace") not in _SEPARATORS:
                out.add(n.text)
        else:
            stack.extend(n.children)
    return frozenset(out)


# -- matching ---------------------------------------------------------------------------------------------------

def _kids(node, *, target: bool) -> list:
    out = []
    for c in node.children:
        if not c.is_named and c.type in _SEPARATORS:
            continue
        if target and "comment" in c.type:
            continue
        out.append(c)
    return out


class _Matcher:
    def __init__(self, src: bytes, deadline: float):
        self.src = src
        self.deadline = deadline
        self.steps = 0

    def text(self, a, b=None) -> str:
        b = b or a
        return self.src[a.start_byte:b.end_byte].decode("utf-8", "replace")

    def tick(self) -> None:
        self.steps += 1
        if self.steps % 512 == 1 and time.monotonic() > self.deadline:
            raise _Timeout

    def bind(self, binds: dict, name: str, value: str) -> bool:
        if not name.strip("_"):
            return True
        if name in binds:
            return binds[name] == value
        binds[name] = value
        return True

    def node(self, p, t, binds: dict) -> bool:
        self.tick()
        mv = _mv(p)
        if mv is not None and not mv[0]:
            return t.is_named and self.bind(binds, mv[1], self.text(t))
        if p.type != t.type:
            return False
        if p.child_count == 0:
            return p.text == t.text
        return self.seq(_kids(p, target=False), 0, _kids(t, target=True), 0, binds)

    def seq(self, ps: list, i: int, ts: list, j: int, binds: dict) -> bool:
        if i == len(ps):
            return j == len(ts)
        mv = _mv(ps[i])
        if mv is not None and mv[0]:
            for k in range(j, len(ts) + 1):
                saved = dict(binds)
                if (self.bind(binds, "$" + mv[1] if mv[1].strip("_") else "", self.text(ts[j], ts[k - 1])
                              if k > j else "") and self.seq(ps, i + 1, ts, k, binds)):
                    return True
                binds.clear()
                binds.update(saved)
            return False
        if j == len(ts):
            return False
        saved = dict(binds)
        if self.node(ps[i], ts[j], binds) and self.seq(ps, i + 1, ts, j + 1, binds):
            return True
        binds.clear()
        binds.update(saved)
        return False


def _short(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= _SHOWN else s[:_SHOWN - 3] + "..."


def search_file(src: bytes, suffix: str, pnode, *, deadline: float, tree=None) -> list[dict]:
    """The matches of the compiled pattern node in one file's bytes (raises ``_Timeout`` past ``deadline``)."""
    tree = tree or _parser(suffix).parse(src)
    m = _Matcher(src, deadline)
    out, stack = [], [tree.root_node]
    while stack:
        n = stack.pop()
        m.tick()
        if n.type == pnode.type:
            binds: dict = {}
            if m.node(pnode, n, binds):
                caps = {(k[1:] if k.startswith("$") else k): _short(v) for k, v in binds.items()}
                out.append({"line": n.start_point[0] + 1, "end": n.end_point[0] + 1,
                            "text": _short(m.text(n)), "captures": caps})
        stack.extend(reversed(n.children))
    return out


# -- rule files -------------------------------------------------------------------------------------------------

def _scalar(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1].replace("\\\"", "\"") if v[0] == '"' else v[1:-1].replace("''", "'")
    return v


def _yaml_doc(lines: list[str]) -> dict:
    """The small YAML subset rule files use: nested mappings by indentation, plain or quoted scalars and ``|``/``>``
    block scalars (no lists, anchors or flow collections)."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        key, sep, val = raw.strip().partition(":")
        if not sep:
            raise ValueError(f"not a `key: value` line: {raw.strip()!r}")
        while stack[-1][0] >= ind:
            stack.pop()
        cur = stack[-1][1]
        val = val.strip()
        if val in ("|", ">", "|-", ">-", "|+", ">+"):
            block = []
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip(" ")) > ind):
                block.append(lines[i])
                i += 1
            pad = min((len(b) - len(b.lstrip(" ")) for b in block if b.strip()), default=0)
            body = [b[pad:] for b in block]
            cur[key.strip()] = ("\n" if val[0] == "|" else " ").join(body).strip()
        elif val == "":
            cur[key.strip()] = {}
            stack.append((ind, cur[key.strip()]))
        else:
            cur[key.strip()] = _scalar(val.split(" #")[0])
    return root


def load_rules(path: Path) -> list[dict]:
    """The rules of a rule file: one per YAML document (``---`` between them)."""
    docs, cur = [], []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        if ln.strip() == "---":
            docs.append(cur)
            cur = []
        else:
            cur.append(ln.rstrip())
    docs.append(cur)
    rules = []
    for k, lines in enumerate(docs):
        if not any(ln.strip() and not ln.lstrip().startswith("#") for ln in lines):
            continue
        d = _yaml_doc(lines)
        rule = d.get("rule") if isinstance(d.get("rule"), dict) else {}
        pattern = d.get("pattern") or rule.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f"{path}: rule {k + 1} has no pattern")
        lang = d.get("language")
        rules.append({"id": str(d.get("id") or f"{Path(path).stem}-{k + 1}"), "pattern": pattern,
                      "langs": [norm_lang(lang)] if isinstance(lang, str) and lang else None,
                      "message": str(d.get("message") or "")})
    return rules


# -- the search -------------------------------------------------------------------------------------------------

def _inside(rel: str, paths: list[str]) -> bool:
    return not paths or any(p in ("", ".") or rel == p or rel.startswith(p.rstrip("/") + "/") for p in paths)


def run(repo: Path, pattern: str | None = None, *, rules: list[dict] | None = None, langs: list[str] | None = None,
        paths: list[str] | None = None, max_results: int = MAX_RESULTS, file_seconds: float = FILE_SECONDS,
        total_seconds: float = TOTAL_SECONDS, files: list[str] | None = None) -> dict:
    """Search the project's files for ``pattern`` (or each rule's pattern); see the module docstring."""
    from verinoda.snapshot import listed_files

    repo = Path(repo)
    jobs = list(rules or [])
    if pattern:
        jobs.insert(0, {"id": None, "pattern": pattern, "langs": None, "message": ""})
    if not jobs:
        raise PatternError("give a pattern or a rule file")
    want = {norm_lang(x) for x in langs} if langs else None
    if want and want - set(known_langs()):
        raise PatternError(f"unknown language(s): {', '.join(sorted(want - set(known_langs())))} "
                           f"(known: {', '.join(known_langs())})")
    table = languages()
    paths = [p.replace("\\", "/").strip("/") for p in (paths or [])]
    cand = [f for f in (files if files is not None else listed_files(repo))
            if Path(f).suffix.lower() in table and _inside(f, paths)]
    cand = [f for f in cand if want is None or lang_name(Path(f).suffix.lower()) in want]
    # which pattern compiles in which language (by suffix): a language that cannot parse it is not searched
    compiled: dict[tuple[int, str], object] = {}
    unparsed: dict[str, list[str]] = {}
    suffixes = sorted({Path(f).suffix.lower() for f in cand})
    for k, job in enumerate(jobs):
        for sfx in suffixes:
            ln = lang_name(sfx)
            if job["langs"] and ln not in job["langs"]:
                continue
            pnode = _compile(job["pattern"], sfx)
            if pnode is not None and _mv(pnode) is not None:
                raise PatternError("the pattern is only a metavariable: it would match every node")
            if pnode is None:
                unparsed.setdefault(job["id"] or "pattern", [])
                if ln not in unparsed[job["id"] or "pattern"]:
                    unparsed[job["id"] or "pattern"].append(ln)
            else:
                compiled[(k, sfx)] = (pnode, _needles(pnode))
    if not compiled and cand:
        langs_tried = sorted({lang_name(s) for s in suffixes})
        raise PatternError(f"the pattern does not parse as {', '.join(langs_tried)} code")
    res: dict = {"status": "none", "pattern": pattern, "rules": [j["id"] for j in jobs if j["id"]],
                 "matches": [], "count": 0, "truncated": False, "files_searched": 0, "by_lang": {},
                 "not_searched": {"timed_out": [], "too_large": 0, "unreadable": [], "budget_spent": 0,
                                  "pattern_not_parsed": unparsed}}
    t_end = time.monotonic() + total_seconds
    for n, f in enumerate(cand):
        sfx = Path(f).suffix.lower()
        mine = [(k, compiled[(k, sfx)]) for k in range(len(jobs)) if (k, sfx) in compiled]
        if not mine:
            continue
        if time.monotonic() > t_end:
            res["not_searched"]["budget_spent"] = len(cand) - n
            break
        try:
            if (repo / f).stat().st_size > MAX_BYTES:
                res["not_searched"]["too_large"] += 1
                continue
            src = (repo / f).read_bytes()
        except OSError:
            res["not_searched"]["unreadable"].append(f)
            continue
        res["files_searched"] += 1
        ln = lang_name(sfx)
        res["by_lang"][ln] = res["by_lang"].get(ln, 0) + 1
        mine = [(k, pnode) for k, (pnode, needles) in mine if all(x in src for x in needles)]
        if not mine:
            continue
        try:
            deadline = min(time.monotonic() + file_seconds, t_end)
            tree = _parser(sfx).parse(src)
            for k, pnode in mine:
                for hit in search_file(src, sfx, pnode, deadline=deadline, tree=tree):
                    res["count"] += 1
                    if len(res["matches"]) >= max_results:
                        res["truncated"] = True
                        continue
                    entry = {"at": f"{f}:{hit['line']}", "end": hit["end"], "lang": ln,
                             "status": "statically_verified", "text": hit["text"], "captures": hit["captures"]}
                    if jobs[k]["id"]:
                        entry["rule"] = jobs[k]["id"]
                        if jobs[k]["message"]:
                            entry["message"] = jobs[k]["message"]
                    res["matches"].append(entry)
        except _Timeout:
            res["not_searched"]["timed_out"].append(f)
    res["status"] = "found" if res["count"] else "none"
    return res


def render(r: dict) -> str:
    what = f"`{r['pattern']}`" if r.get("pattern") else ""
    if r.get("rules"):
        what = (what + " and " if what else "") + f"rule(s) {', '.join(r['rules'])}"
    langs = ", ".join(f"{k} {v}" for k, v in sorted(r["by_lang"].items()))
    out = [f"grep-ast: {r['count']} match(es) of {what} in {r['files_searched']} file(s)"
           + (f" ({langs})" if langs else "")]
    for m in r["matches"]:
        caps = "  ".join(f"${k}={v!r}" for k, v in m["captures"].items())
        tag = f"[{m['rule']}] " if m.get("rule") else ""
        out.append(f"  {m['at']}  {tag}{m['text']}" + (f"   {caps}" if caps else ""))
        if m.get("message"):
            out.append(f"      {m['message']}")
    if r["truncated"]:
        out.append(f"  ... {r['count'] - len(r['matches'])} more (--max-results)")
    ns = r["not_searched"]
    for rid, ls in ns["pattern_not_parsed"].items():
        out.append(f"not searched: {', '.join(ls)} (the grammar does not parse {rid})")
    if ns["timed_out"]:
        out.append(f"not finished (time bound per file): {', '.join(ns['timed_out'][:10])}"
                   + (" ..." if len(ns["timed_out"]) > 10 else ""))
    if ns["too_large"]:
        out.append(f"skipped: {ns['too_large']} file(s) over 1 MB")
    if ns["unreadable"]:
        out.append(f"unreadable: {', '.join(ns['unreadable'][:10])}")
    if ns["budget_spent"]:
        out.append(f"not searched: {ns['budget_spent']} file(s) (the time budget of the search ran out)")
    out.append("Each match is statically_verified: the matched text is in the file now. Shape only: names are "
               "not resolved.")
    return "\n".join(out)
