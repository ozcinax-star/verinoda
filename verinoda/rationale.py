"""Rationale comments attached to the code they explain.

A ``# WHY:``, ``// NOTE:``, ``HACK:``, ``IMPORTANT:`` comment, or one that cites a decision record (``ADR-12``,
``docs/adr/0003-cache.md``, ``docs/DESIGN.md D50``, "decision record"), is the author's own answer to "why is
this code the way it is". :func:`file_rationale` finds them in one file with the comment lines that follow them,
and attaches each to the definition it sits directly above (comment and decorator lines may come between) or
else to the innermost definition it sits in, else to the module. Definitions come from the file's facts
(:mod:`verinoda.anchors`: Python, TypeScript/JavaScript) or, for other languages, from the graph's symbol spans.

Nothing is stored: the comments are read from the file as it is now, so the index build and ``graph.json`` stay
as they are. :func:`for_node` gives node_inspect the ones attached to one node, ``verinoda rationale`` lists them
for a symbol, a path or the project.

What the output states as fact is that the comment is at that line (``statically_verified``); what the comment
says is the author's statement, not checked, and which definition it explains is read from its position
(``attach_status``: ``strong_inference``). A symbol whose file changed since the index gets none: its line is the
index's.
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

STATUS = "statically_verified"
ATTACH_STATUS = "strong_inference"  # which definition a comment explains: read from its position
NOTE = ("status: the comment is at that line; what it says is its author's statement (not verified); "
        "attach (the definition it explains) is read from its position (attach_status)")
STALE_NOTE = "the file changed since the index: run index_update (verinoda scan) to attach its rationale comments"
MAX_BLOCK = 6        # lines of one comment block
MAX_TEXT = 300       # characters of one block's quoted text

_PY = (".py", ".pyi")
_HASH = (".sh", ".rb", ".ps1", ".pl", ".r", ".yaml", ".yml", ".toml", ".cmake", ".mcfunction")
_DASH = (".lua", ".sql", ".hs")
_SLASH = (".java", ".kt", ".kts", ".groovy", ".scala", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs",
          ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".php", ".dart", ".fsh", ".vsh", ".glsl", ".gradle")
CODE_SUFFIXES = frozenset(_PY + _HASH + _DASH + _SLASH)

_MARK = re.compile(r"^@?(why|note|nb|n\.b\.|hack|important|rationale)\b\s*(?:\([^)]*\))?\s*:", re.I)
_CITE = re.compile(r"\bADR[-_ #]?\d+\b"
                   r"|(?<![\w./-])(?:[\w.-]+/)*(?:adrs?|decisions|decision-records)/[\w.-]+"
                   r"\.(?:md|markdown|rst|txt|adoc)\b"
                   r"|(?<![\w./-])[\w./-]+\.md\s+D\d+\b"
                   r"|\bdecision records?\b", re.I)
# a superset of what :data:`_MARK` and :data:`_CITE` find, to skip a file without reading its comments
_QUICK = re.compile(r"why|note|n\.?b\b|hack|important|rationale|adr|decision|\.md\s+D\d", re.I)


def _body(text: str) -> str:
    """A comment line's words, without its markers (``#``, ``//``, ``/**``, ``*``, ``--``, ``*/``)."""
    s = text.strip()
    s = re.sub(r"^(?:#+!?|//+!?|/\*+!?|\*+(?!/)|--+)", "", s).strip()
    return re.sub(r"\*+/$", "", s).strip()


def _comment_lines(text: str, suffix: str) -> dict[int, tuple[str, bool]]:
    """line -> (comment text as written, standalone: nothing but the comment on the line)."""
    lines = text.split("\n")  # only \n ends a line, as for editors and git (not U+2028, \f ...)
    out: dict[int, tuple[str, bool]] = {}
    if suffix in _PY:
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.COMMENT:
                    ln, col = tok.start
                    out[ln] = (tok.string, not lines[ln - 1][:col].strip())
            return out
        except (tokenize.TokenError, IndentationError, SyntaxError):
            out = {}  # does not tokenize: read it line by line as below
    prefixes = ("#",) if suffix in _PY + _HASH else ("--",) if suffix in _DASH else ("//", "/*")
    in_block = False
    for k, line in enumerate(lines, 1):
        s = line.strip()
        if in_block:  # the rest of a /* ... */ block
            out[k] = (s, True)
            in_block = "*/" not in s
            continue
        if not s.startswith(prefixes) or (k == 1 and s.startswith("#!")):
            continue
        out[k] = (s, True)
        in_block = s.startswith("/*") and "*/" not in s[2:]
    return out


def _symbols(rel: str, text: str, root: Path | None, graph) -> tuple[dict[str, dict], str]:
    """Definitions of the file: ``{name: {"start", "end", "nid"?}}`` and where they came from."""
    from verinoda import anchors

    if root is not None:
        facts, _ = anchors.facts_for_path(Path(root) / rel, rel)
        if facts and facts.get("symbols") is not None and facts.get("lang") != "markdown":
            syms = {q: {"start": s["start"], "end": s["end"], "def": s.get("def") or s["start"]}
                    for q, s in facts["symbols"].items()}
            if graph is not None and facts.get("lang") != "python":
                # the facts of TS/JS miss some graph symbols (``const f = () => ...``): add those
                for nid in graph.symbols_in(rel):
                    sp = graph.span(nid)
                    if sp and not any(s["start"] <= sp[0] <= s["def"] for s in syms.values()):
                        syms[nid] = {"start": sp[0], "end": sp[1], "nid": nid, "label": graph.label(nid)}
            return syms, "facts"
    syms: dict[str, dict] = {}
    if graph is not None:
        for nid in graph.symbols_in(rel):
            sp = graph.span(nid)
            if sp:
                syms[nid] = {"start": sp[0], "end": sp[1], "nid": nid, "label": graph.label(nid)}
        if syms:
            return syms, "graph"
    return syms, "none"


def _innermost(syms: dict[str, dict], a: int, b: int) -> str | None:
    best = None
    for q, s in syms.items():
        if s["start"] <= a and b <= s["end"] and (best is None or s["end"] - s["start"] < syms[best]["end"]
                                                   - syms[best]["start"]):
            best = q
    return best


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _py_tail_owner(syms: dict[str, dict], lines: list[str], a: int, q: str | None) -> str | None:
    """A Python comment after a body's last statement (ast spans end there): the definition just above it
    whose ``def`` line is indented less than the comment, with only blank and comment lines between."""
    ind = _indent(lines[a - 1])
    best = None
    for name, s in syms.items():
        head = s.get("def", s["start"])
        if not (s["end"] < a and head <= len(lines) and _indent(lines[head - 1]) < ind):
            continue
        if any(lines[i - 1].strip() and not lines[i - 1].lstrip().startswith("#") for i in range(s["end"] + 1, a)):
            continue
        if q is not None and not (syms[q]["start"] <= s["start"] and s["end"] <= syms[q]["end"]):
            continue
        rank = (_indent(lines[head - 1]), -(s["end"] - s["start"]))
        if best is None or rank > best[0]:
            best = (rank, name)
    return best[1] if best else None


def scan_text(rel: str, text: str, syms: dict[str, dict] | None = None) -> list[dict]:
    """The rationale comments of one file's text, each attached to a definition of ``syms``."""
    suffix = Path(rel).suffix.lower()
    if suffix not in CODE_SUFFIXES:
        return []
    syms = syms or {}
    lines = text.split("\n")
    comments = _comment_lines(text, suffix)
    out: list[dict] = []
    used: set[int] = set()
    for ln in sorted(comments):
        if ln in used:
            continue
        raw, alone = comments[ln]
        body = _body(raw)
        mark = _MARK.match(body)
        cites = _CITE.findall(body)
        if not mark and not cites:
            continue
        block, k = [ln], ln + 1
        while (alone and k in comments and comments[k][1] and len(block) < MAX_BLOCK and _body(comments[k][0])
               and not _MARK.match(_body(comments[k][0]))):
            block.append(k)
            k += 1
        used.update(block)
        quoted = [comments[i][0].strip() for i in block]
        for i in block[1:]:
            cites += _CITE.findall(_body(comments[i][0]))
        a, b = block[0], block[-1]
        tag = (mark.group(1).upper().replace(".", "") if mark else "ADR")
        q, how = None, "module"
        if alone:  # directly above a definition: only comment and decorator/annotation lines between
            j = b + 1
            while j <= len(lines) and (j in comments and comments[j][1] or lines[j - 1].lstrip().startswith("@")):
                j += 1
            if j <= len(lines) and lines[j - 1].strip():
                above = sorted((s["start"], -(s["end"] - s["start"]), name) for name, s in syms.items()
                               if b < s["start"] <= j)
                if above:
                    q, how = above[0][2], "above"
        if q is None:
            q = _innermost(syms, a, b)
            if suffix in _PY and syms:
                q = _py_tail_owner(syms, lines, a, q) or q
            # between a decorator and its ``def`` line is above the definition too
            how = "module" if not q else "above" if b < syms[q].get("def", syms[q]["start"]) else "inside"
        text_q = "\n".join(quoted)
        row = {"at": f"{rel}:{a}" + (f"-{b}" if b != a else ""), "tag": tag,
               "text": clip(text_q, MAX_TEXT), "attach": how,
               "symbol": (syms[q].get("label") or q) if q else None, "status": STATUS,
               "attach_status": ATTACH_STATUS}
        if cites:
            row["cites"] = list(dict.fromkeys(c.strip() for c in cites))[:4]
        row["_key"] = q
        out.append(row)
    return out


def clip(text: str, n: int) -> str:
    """``text`` cut to ``n`` characters, ending in ``...`` when cut."""
    return text if len(text) <= n else text[:n - 3] + "..."


def file_rationale(root: Path, rel: str, graph=None) -> list[dict]:
    """The rationale comments of ``rel`` as it is on disk under ``root`` (``_key``: the definition's key)."""
    rel = rel.replace("\\", "/")
    if Path(rel).suffix.lower() not in CODE_SUFFIXES:
        return []
    try:
        text = (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # most files have none: parse a file for its definitions only when its comments have one
    if not _QUICK.search(text) or not scan_text(rel, text, {}):
        return []
    syms, _ = _symbols(rel, text, root, graph)
    return scan_text(rel, text, syms)


def public(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def for_node(g, nid: str) -> list[dict]:
    """The rationale comments attached to one graph node: above or inside its definition (a file node: those at
    module level)."""
    f = g.file(nid)
    if not f or Path(f).suffix.lower() not in CODE_SUFFIXES:
        return []
    try:
        text = (Path(g.root) / f).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    syms, src = _symbols(f, text, g.root, g)
    rows = scan_text(f, text, syms)
    if g.is_file_node(nid):
        return public([r for r in rows if r["_key"] is None])
    if src == "graph":
        key = nid if nid in syms else None
    else:
        line = g.line(nid)
        key = None
        if line:
            # the definition whose header holds the node's line (decorators start it earlier)
            key = _innermost(syms, line, line)
    return public([r for r in rows if key is not None and r["_key"] == key])


# -----------------------------------------------------------------------------
# verinoda rationale
# -----------------------------------------------------------------------------

def _listed(repo: Path) -> list[str]:
    """Tracked and untracked-not-ignored files (``git ls-files -z``: non-ASCII names as they are)."""
    from verinoda.snapshot import listed_files

    return sorted(f.replace("\\", "/") for f in listed_files(repo) if ".verinoda" not in f.split("/"))


def _rel_target(repo: Path, target: str) -> tuple[str | None, bool]:
    """``target`` as a repository-relative POSIX path (``""``: the whole project) and whether it exists;
    ``(None, True)``: an existing path outside the repository."""
    t = target.strip()
    if not t:
        return "", True
    p = Path(t)
    full = p if p.is_absolute() else Path(repo) / p
    if not full.exists():
        return t.replace("\\", "/").rstrip("/"), False
    try:
        rel = full.resolve().relative_to(Path(repo).resolve()).as_posix()
    except ValueError:
        return None, True
    return ("" if rel == "." else rel), True


def lookup(repo: Path, target: str | None = None, *, graph=None, stale=None, limit: int = 50) -> dict:
    """``verinoda rationale [TARGET]``: a symbol -> the comments attached to it; a file or folder (or nothing:
    the project) -> every rationale comment there, by file."""
    repo = Path(repo)
    load_error = None
    try:
        g = graph() if callable(graph) else graph
    except Exception as exc:  # noqa: BLE001 - no usable index: files with facts still work, symbols do not resolve
        g, load_error = None, f"{type(exc).__name__}: {exc}"[:200]
    t, exists = _rel_target(repo, target or "")
    base = {"target": target or ".", "note": NOTE}
    if t is None:
        return {**base, "status": "not_found", "rows": [], "note": f"{target!r} is outside the repository"}
    if t and not exists:
        if g is None:
            why = (f"the index could not be loaded ({load_error}; run `verinoda scan`)" if load_error else
                   "there is no index to resolve a symbol (run `verinoda scan`)")
            return {**base, "status": "not_found", "rows": [], "note": f"{t!r} is not a file or folder, and {why}"}
        from verinoda import naming

        stale_files = list(stale() if callable(stale) else (stale or ()))
        r = naming.resolve(g, t, stale=stale_files)
        if not (r.exact and r.node and g.file(r.node)):
            return {**base, "status": "ambiguous" if r.status == naming.AMBIGUOUS else "not_found", "rows": [],
                    "note": r.note or f"{t!r} is not a file, a folder or a symbol",
                    "candidates": [{"label": g.label(c), "at": f"{g.file(c)}:{g.line(c)}"}
                                   for c in list(r.candidates)[:8]]}
        span = g.span(r.node)
        at = f"{g.file(r.node)}:{span[0]}-{span[1]}" if span else f"{g.file(r.node)}:{g.line(r.node)}"
        sym = {"kind": "symbol", "symbol": g.label(r.node), "at": at}
        if g.file(r.node) in stale_files:  # its line is the index's: another definition's comments would show
            return {**base, **sym, "status": "stale", "rows": [], "total": 0, "note": STALE_NOTE}
        rows = for_node(g, r.node)
        return {**base, **sym, "status": "found" if rows else "none", "rows": rows, "total": len(rows)}
    if t and (repo / t).is_file():
        files = [t]
    else:
        files = [f for f in _listed(repo) if not t or f == t or f.startswith(t + "/")]
    rows: list[dict] = []
    for f in files:
        rows += public(file_rationale(repo, f, g))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["tag"]] = counts.get(r["tag"], 0) + 1
    return {**base, "status": "found" if rows else "none", "kind": "path", "files_read": len(files),
            "counts": dict(sorted(counts.items())), "total": len(rows), "rows": rows[:max(1, limit)],
            "truncated": len(rows) > max(1, limit)}


def render(res: dict) -> str:
    if res["status"] == "stale":
        return f"{res['symbol']} ({res['at']}): {res['note']}"
    if res["status"] in ("not_found", "ambiguous"):
        out = [f"{res['target']}: {res['status']} - {res['note']}"]
        out += [f"  - {c['label']} ({c['at']})" for c in res.get("candidates") or []]
        return "\n".join(out)
    if res.get("kind") == "symbol":
        head = f"{res['symbol']} ({res['at']}): {res['total']} rationale comment(s)"
    else:
        counts = ", ".join(f"{k} {v}" for k, v in res["counts"].items())
        head = (f"{res['target']}: {res['total']} rationale comment(s) in {res['files_read']} file(s)"
                + (f" ({counts})" if counts else ""))
    out = [head]
    if not res["rows"]:
        out.append("  no WHY/NOTE/NB/HACK/IMPORTANT/RATIONALE comment and none citing a decision record")
    for r in res["rows"]:
        where = {"above": f"above {r['symbol']}", "inside": f"in {r['symbol']}", "module": "module level"}[r["attach"]]
        lines = r["text"].split("\n")
        out.append(f"  {r['at']} {r['tag']} ({where}): {lines[0]}")
        out += [f"      {x}" for x in lines[1:]]
    if res.get("truncated"):
        out.append(f"  (+{res['total'] - len(res['rows'])} more: --limit N or --json)")
    out.append(" " + res["note"])
    return "\n".join(out)
