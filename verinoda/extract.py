"""The whole definition around a location: ``verinoda extract src/a.py:40``.

A location comes in as ``path:LINE`` (``path:LINE-LINE``, ``path:LINE:COL``), ``path#Symbol`` (or
``path::Symbol``; ``m`` finds ``C.m``), or as the text of a compiler, linter or test run (``--from FILE``): Python
traceback lines, ``path:LINE[:COL]`` (gcc, javac, rustc, pytest, eslint), ``path(LINE,COL)`` (tsc, MSBuild),
``path:[LINE,COL]`` (Maven) and JVM stack frames (``at a.b.C.m(C.java:40)``, found by their package path). What
comes out is the innermost function or class that contains the line, whole, with its lines: the definitions of
:mod:`verinoda.anchors` (Python's own parser; tree-sitter for the other languages it has a grammar for), a
top-level statement when the line is in no definition, a Markdown section for a document.

No index is read: the file is parsed as it is on disk now. Each definition found is a claim with its lines as
evidence: ``statically_verified`` from a clean parse, ``strong_inference`` when tree-sitter had to recover from
syntax errors (the span it recovered may be wrong). A path that is not the project's (the standard library, a
dependency's frame) is counted, not searched for.
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_LOCATIONS = 20
# explicit targets
_AT_LINE = re.compile(r"^(?P<path>.+?):(?P<a>\d+)(?:-(?P<b>\d+)|:\d+)?:?$")
_AT_SYMBOL = re.compile(r"^(?P<path>.+?\.\w+)(?:#|::)(?P<sym>[^\s#:][^\s]*)$")
# locations in a tool's output, tried in this order on each line (the first pattern that matches a line wins)
_OUTPUT = (
    ("py", re.compile(r"File \"(?P<path>[^\"]+)\", line (?P<line>\d+)")),
    ("jvm", re.compile(r"\bat\s+(?:[\w.$@-]+/{1,2})*(?P<cls>[\w$.]+)\.[\w$<>]+\((?P<file>[\w$-]+\.\w+):"
                       r"(?P<line>\d+)\)")),
    ("maven", re.compile(r"(?P<path>(?:[A-Za-z]:)?[^\s\"'()\[\]:]*\.\w+):\[(?P<line>\d+),\d+\]")),
    ("msbuild", re.compile(r"(?P<path>(?:[A-Za-z]:)?[^\s\"'()\[\]:]*\.\w+)\((?P<line>\d+)(?:,\d+)*\)")),
    ("unix", re.compile(r"(?<![\w/\\.~-])(?P<path>(?:[A-Za-z]:)?[\w./\\~-]*\w\.[A-Za-z]\w{0,9}):(?P<line>\d+)"
                        r"(?![\d.])")),
)


def parse_target(target: str) -> dict | None:
    """``{"path", "line", "end"}`` or ``{"path", "symbol"}`` for an explicit target; None for anything else."""
    t = target.strip().strip("\"'")
    m = _AT_LINE.match(t)
    if m:
        a = int(m.group("a"))
        return {"path": m.group("path"), "line": a, "end": max(a, int(m.group("b"))) if m.group("b") else a}
    m = _AT_SYMBOL.match(t)
    if m:
        return {"path": m.group("path"), "symbol": m.group("sym")}
    return None


def locations(text: str) -> list[dict]:
    """The locations a compiler's, linter's or test run's output names, in order, each once."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for raw in text.splitlines():
        for kind, rx in _OUTPUT:
            hits = list(rx.finditer(raw))
            if not hits:
                continue
            for m in hits:
                ln = int(m.group("line"))
                if kind == "jvm":
                    # the package path of the frame's class (an inner class `Outer$Inner` is in Outer's file)
                    pkg = []
                    for part in m.group("cls").split("."):
                        if not part or not part[0].islower():
                            break
                        pkg.append(part)
                    loc = {"path": "/".join([*pkg, m.group("file")]), "line": ln, "end": ln, "suffix": True}
                else:
                    loc = {"path": m.group("path"), "line": ln, "end": ln}
                key = (loc["path"], ln)
                if ln > 0 and key not in seen:
                    seen.add(key)
                    out.append({**loc, "input": m.group(0).strip()})
            break
    return out


def resolve_path(repo: Path, path: str, *, cwd: Path | None = None, suffix: bool = False,
                 files=None) -> tuple[str | None, list[str]]:
    """``(repo-relative path, [])`` for a file of the project; ``(None, candidates)`` when several files end
    with ``path``; ``(None, [])`` when it is not the project's. ``suffix``: only match the end of a path (a
    JVM frame's package path)."""
    repo = Path(repo).resolve()
    p = Path(path.replace("\\", "/"))
    # a POSIX absolute path is absolute on Windows too (a CI log, a Linux traceback)
    absolute = p.is_absolute() or path.startswith(("/", "\\"))
    if not suffix:
        tries = [p] if absolute else [Path(cwd or Path.cwd()) / p, repo / p]
        for c in tries:
            try:
                c = c.resolve()
                rel = c.relative_to(repo)
            except (OSError, ValueError):
                continue
            if rel.parts and rel.parts[0] in (".git", ".verinoda"):
                continue
            if c.is_file():
                return rel.as_posix(), []
        if absolute:  # another machine's or a dependency's file: never guessed from its name
            return None, []
    # a relative path from another working directory (javac run in a module, a bare file name): the one file
    # of the project whose path ends with it
    want = re.sub(r"^(?:\./)+", "", p.as_posix())
    if not want or want.startswith("../"):
        return None, []
    if files is None:
        from verinoda.snapshot import listed_files

        files = listed_files(repo)
    hits = [f for f in files if f == want or f.endswith("/" + want)]
    if len(hits) == 1:
        return hits[0], []
    return None, hits[:8]


def _commit(repo: Path) -> str | None:
    try:
        from verinoda.treestate import head_commit

        return head_commit(repo)
    except Exception:  # noqa: BLE001 - not a git work tree: evidence without a commit
        return None


def _parse_errors(path: Path, rel: str) -> bool:
    """Whether tree-sitter had to recover from syntax errors in this file (never for Python or Markdown)."""
    from verinoda import anchors

    parser = anchors._ts_parser(Path(rel).suffix.lower())
    if parser is None:
        return False
    try:
        return bool(parser.parse(path.read_bytes()).root_node.has_error)
    except (OSError, ValueError):
        return False


def _display(key: str) -> str:
    return key.split("#")[0]


def _sym_def(key: str, s: dict) -> dict:
    name = _display(key)
    parts = name.split(".")
    return {"name": name, "kind": s["kind"], "start": s["start"], "end": s["end"],
            "inside": [".".join(parts[:k]) for k in range(1, len(parts))]}


def _pick(facts: dict, a: int, b: int) -> dict | None:
    """The innermost definition around lines ``a..b``, else the top-level statement or the section."""
    from verinoda import anchors

    hit = anchors.enclosing(facts, a, b)
    if hit is None:
        return None
    kind, key = hit
    if kind == "sym":
        return _sym_def(key, facts["symbols"][key])
    if kind == "mod":
        m = facts["module"][key]
        # `ASSIGN:LIMIT` and `IMPORT:os,re` name what they bind; other statements are keyed by a hash
        return {"name": key.partition(":")[2] if key.startswith(("ASSIGN:", "IMPORT:")) else "",
                "kind": "statement", "start": m["start"], "end": m["end"], "inside": []}
    s = facts["sections"][key]
    title = s.get("title") or key
    parent = key[:-len(title) - 1] if key.endswith("/" + title) else ""  # the headings above it, "/"-joined
    return {"name": title, "kind": "section", "start": s["start"], "end": s["end"],
            "inside": [parent] if parent else []}


def extract_one(repo: Path, loc: dict, *, cwd: Path | None = None, commit: str | None = None,
                max_lines: int | None = None, files=None) -> dict:
    """One location -> its enclosing definition (``status`` found), or why there is none: ``not_found`` (no
    such file, line or symbol), ``ambiguous`` (several files or symbols; see ``candidates``), ``no_definition``
    (the line is between definitions), ``unsupported`` (no parser for the language, or the file does not
    parse)."""
    from verinoda import anchors, evidence

    repo = Path(repo).resolve()
    shown = loc.get("input") or (f"{loc['path']}#{loc['symbol']}" if "symbol" in loc else
                                 f"{loc['path']}:{loc['line']}" + (f"-{loc['end']}" if loc["end"] != loc["line"]
                                                                   else ""))
    base = {"input": shown}
    rel, cands = resolve_path(repo, loc["path"], cwd=cwd, suffix=bool(loc.get("suffix")), files=files)
    if rel is None:
        if cands:
            return {**base, "status": "ambiguous", "note": f"{len(cands)} files end with {loc['path']}",
                    "candidates": cands}
        return {**base, "status": "not_found", "note": f"{loc['path']} is not a file of this project"}
    facts, text = anchors.facts_for_path(repo / rel, rel)
    base["file"] = rel
    if text is None:
        return {**base, "status": "not_found", "note": f"{rel} cannot be read"}
    lines = text.splitlines()
    if facts is None:
        why = ("it does not parse" if anchors.scheme_for(rel) else
               f"no parser for {Path(rel).suffix or 'files without an extension'}")
        return {**base, "status": "unsupported", "note": f"no definitions read from {rel}: {why}"}
    if "symbol" in loc:
        named = anchors.symbols_named(facts, loc["symbol"])
        if not named:
            return {**base, "status": "not_found", "note": f"{rel} defines no {loc['symbol']!r}"}
        if len(named) > 1:
            return {**base, "status": "ambiguous", "note": f"{rel} defines {loc['symbol']!r} {len(named)} times",
                    "candidates": [f"{rel}:{s['start']}-{s['end']} {_display(q)}" for q, s in named[:8]]}
        d = _sym_def(*named[0])
        a = b = None
    else:
        a, b = loc["line"], loc["end"]
        if b > len(lines) or a < 1:
            return {**base, "status": "not_found", "note": f"{rel} has {len(lines)} lines"}
        base["line"] = a if a == b else [a, b]
        d = _pick(facts, a, b)
        if d is None:
            return {**base, "status": "no_definition",
                    "note": f"{rel}:{a}" + (f"-{b}" if b != a else "") + " is in no definition, top-level "
                            "statement or section"}
    start, end = d["start"], d["end"]
    cut = end if not max_lines or end - start + 1 <= max_lines else start + max_lines - 1
    recovered = _parse_errors(repo / rel, rel)
    what = {"def": "function", "class": "class", "statement": "top-level statement", "section": "section"}[d["kind"]]
    span = f"{rel}:{start}-{end}"
    named = f"{what} {d['name']}" if d["name"] else what
    claim_text = (f"{rel}:{a}" + (f"-{b}" if b != a else "") + f" is inside {named} ({span})"
                  if a is not None else f"{d['name']} is a {what} at {span}")
    ev = evidence.source_evidence(repo, rel, start, end, commit=commit, anchor=False)
    claim = {"text": claim_text, "status": "strong_inference" if recovered else "statically_verified",
             "evidence": [{k: ev[k] for k in ("locator", "path", "line_start", "line_end", "commit_sha",
                                              "content_hash")}] if ev else []}
    if recovered:
        claim["uncertainties"] = [f"{rel} has syntax errors; the parser recovered, so the span may be wrong"]
    return {**base, "status": "found", "definition": d, "text": "\n".join(lines[start - 1:cut]),
            "truncated": cut < end, "claim": claim}


def run(repo: Path, targets: list[str] | None = None, *, output: str | None = None, cwd: Path | None = None,
        max_lines: int | None = None, limit: int = MAX_LOCATIONS) -> dict:
    """``verinoda extract``: each target (``path:LINE``, ``path#Symbol``; any other text is read as tool
    output) and each location in ``output`` -> :func:`extract_one`. ``not_in_project`` counts the locations of
    ``output`` that are not the project's files (library frames); they are left out of ``results``."""
    repo = Path(repo).resolve()
    commit = _commit(repo)
    locs: list[dict] = []
    for t in targets or []:
        one = parse_target(t)
        if one is not None:
            locs.append(one)
            continue
        found = locations(t)
        if not found:
            locs.append({"path": t, "line": 0, "end": 0, "input": t, "bad": True})
        locs += found
    from_output = [{**loc, "from_output": True} for loc in locations(output)] if output else []
    results: list[dict] = []
    outside: list[str] = []
    left = 0  # locations past ``limit``: counted, not read
    for loc in locs + from_output:
        if len(results) >= limit:
            left += 1
            continue
        if loc.get("bad"):
            results.append({"input": loc["input"], "status": "not_found",
                            "note": "expected path:LINE, path:LINE-LINE, path#Symbol or a compiler's error line"})
            continue
        r = extract_one(repo, loc, cwd=cwd, commit=commit, max_lines=max_lines)
        if loc.get("from_output") and r["status"] == "not_found" and "file" not in r:
            outside.append(loc["input"])
            continue
        results.append(r)
    found = sum(1 for r in results if r["status"] == "found")
    return {"status": "found" if results and found == len(results) else ("partial" if found else "not_found"),
            "results": results, **({"truncated": True, "locations_left": left} if left else {}),
            **({"not_in_project": len(outside), "not_in_project_first": outside[:5]} if outside else {}),
            **({"note": "no location of this project in the output"} if output is not None and not locs
               and not results else {})}


def render(res: dict, *, numbers: bool = True) -> str:
    out: list[str] = []
    for r in res["results"]:
        if r["status"] != "found":
            out.append(f"{r['input']}: {r['status']}" + (f" - {r['note']}" if r.get("note") else ""))
            out += [f"  - {c}" for c in r.get("candidates") or []]
            continue
        d, c = r["definition"], r["claim"]
        where = f"{r['file']}:{d['start']}-{d['end']}"
        tag = "" if c["status"] == "statically_verified" else f" [{c['status']}: {'; '.join(c['uncertainties'])}]"
        out.append(f"{where} {d['kind']}" + (f" {d['name']}" if d["name"] else "")
                   + (f" (for {r['input']})" if r["input"] != where else "") + tag)
        rows = r["text"].split("\n")
        w = len(str(d["start"] + len(rows) - 1))
        out += [f"{d['start'] + k:>{w}} {row}" if numbers else row for k, row in enumerate(rows)]
        if r["truncated"]:
            out.append(f"... ({d['end'] - d['start'] + 1} lines; --max-lines 0 prints them all)")
    if res.get("truncated"):
        out.append(f"(+{res['locations_left']} more locations, not read: --limit)")
    if res.get("not_in_project"):
        out.append(f"{res['not_in_project']} location(s) not in this project, such as "
                   + ", ".join(res["not_in_project_first"][:3]))
    if res.get("note"):
        out.append(res["note"])
    return "\n".join(out)
