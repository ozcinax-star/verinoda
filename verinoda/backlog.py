"""Backlog item numbers in code comments, both ways (docs/DESIGN.md D50).

Projects that keep a numbered backlog (``## 33 - Title`` sections, ``| 69.3 | **Title** | why and what was done |``
table rows) cite its items in code comments (``// 69.3: ...``, ``// why: 67.29 - ...``, ``/** 33: ... */``).
That comment is the shortest path to *why* a line is the way it is, and the item is the shortest path to *where*
a decision lives in the code. :func:`items` reads the backlog, :func:`sites` finds the comments that cite each
item, :func:`items_for` answers "which items explain these lines": the comments on and just above them, and the
declaration comments of the fields they use (``bodyRef.get()`` -> the ``// why: 12.3 - ...`` above
``bodyRef``).

Configurable in ``.verinoda/config.json``: ``"backlog": {"files": ["docs/BACKLOG.md"]}`` (default: the first of
:data:`DEFAULT_FILES` that exists). A dotted number (``69.3``) counts anywhere in a comment, a whole number
(``33``) only as the comment's leading label (``// 33:``, ``/** why: 33 -``); either only when the backlog has
that item.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FILES = ("docs/BACKLOG.md", "BACKLOG.md", "docs/backlog.md", "backlog.md", "docs/TODO.md", "TODO.md")
_ROW = re.compile(r"^\|\s*(\d+(?:\.\d+){0,2})\s*\|(.*)$")
_HEADING = re.compile(r"^#{1,4}\s+(\d+(?:\.\d+){0,2})\s*[-–—:.]\s+(.+?)\s*$")
_COMMENT = re.compile(r"//+|/\*+|(?<!\S)\*(?!/)|(?<!\S)#+(?=\s)|(?<!\S)--(?=\s)|<!--")
_DOTTED = re.compile(r"(?<![\w.])(\d+\.\d+(?:\.\d+)?)(?![\w]|\.\d)")
_LABEL = re.compile(r"^\s*(?:why:?\s*)?(?:<b>)?(\d+(?:\.\d+){0,2})(?:</b>)?\s*[:–—-]")
_CODE_SUFFIXES = {".java", ".kt", ".kts", ".groovy", ".scala", ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".go",
                  ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".lua", ".fsh", ".vsh", ".glsl", ".mcfunction",
                  ".gradle", ".sh", ".ps1", ".rb", ".php"}


@dataclass(frozen=True)
class Item:
    id: str
    file: str
    line: int
    title: str
    text: str      # a row's cells after the id (markdown kept); a heading's own line

    @property
    def at(self) -> str:
        return f"{self.file}:{self.line}"


def backlog_files(repo: Path) -> list[str]:
    from verinoda.paths import load_config

    try:
        cfg = (load_config(repo) or {}).get("backlog") or {}
    except Exception:  # noqa: BLE001 - an unreadable config: the defaults
        cfg = {}
    files = cfg.get("files") if isinstance(cfg, dict) else None
    if isinstance(files, str):
        files = [files]
    if files:
        return [f for f in files if isinstance(f, str) and (Path(repo) / f).is_file()]
    first = next((f for f in DEFAULT_FILES if (Path(repo) / f).is_file()), None)
    return [first] if first else []


def items(repo: Path) -> dict[str, Item]:
    """The numbered items of the backlog: table rows ``| <id> | <title> | ... |`` and headings ``## <id> - ...``."""
    repo = Path(repo)
    out: dict[str, Item] = {}
    for f in backlog_files(repo):
        try:
            lines = (repo / f).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            m = _ROW.match(line)
            if m:
                cells = [c.strip() for c in m.group(2).split("|")]
                title = re.sub(r"[*_`]", "", cells[0]) if cells else ""
                out.setdefault(m.group(1), Item(m.group(1), f, i, title, " | ".join(c for c in cells[1:] if c)))
                continue
            m = _HEADING.match(line)
            if m:
                out.setdefault(m.group(1), Item(m.group(1), f, i, m.group(2), m.group(2)))
    return out


def comment_ids(line: str, known) -> list[str]:
    """Item ids a line's comment cites that the backlog has."""
    m = _COMMENT.search(line)
    if not m:
        return []
    text = line[m.end():]
    found = [i for i in _DOTTED.findall(text) if i in known]
    lab = _LABEL.match(text)
    if lab and lab.group(1) in known:
        found.insert(0, lab.group(1))
    return list(dict.fromkeys(found))


def sites(repo: Path, files: list[str], table: dict[str, Item] | None = None) -> dict[str, list[tuple[str, int]]]:
    """Item id -> the code sites (file, line) whose comments cite it, over ``files``."""
    repo = Path(repo)
    table = cached_items(repo) if table is None else table
    out: dict[str, list[tuple[str, int]]] = {}
    if not table:
        return out
    for f in files:
        if Path(f).suffix.lower() not in _CODE_SUFFIXES:
            continue
        try:
            lines = (repo / f).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for rid in comment_ids(line, table):
                out.setdefault(rid, []).append((f, i))
    return out


def _is_comment(line: str) -> bool:
    s = line.lstrip()
    return s.startswith(("//", "/*", "*", "#", "--")) or s.endswith("*/")


def _cited_above(lines: list[str], a: int, table, context: int) -> list[tuple[str, int]]:
    """Ids cited on line ``a`` and the comment lines just above it (a ``// why:`` sits above its code)."""
    out: list[tuple[str, int]] = [(rid, a) for rid in comment_ids(lines[a - 1], table)] if 0 < a <= len(lines) else []
    k = a - 1
    while k >= 1 and a - k <= context and _is_comment(lines[k - 1]):
        out += [(rid, k) for rid in comment_ids(lines[k - 1], table)]
        k -= 1
    return out


def items_for(repo: Path, file: str, a: int, b: int | None = None, *, table: dict[str, Item] | None = None,
              context: int = 4) -> list[dict]:
    """The items that explain lines ``a..b`` of ``file``: ``{"item", "line", "via"}``, nearest first. ``via`` is
    None for a comment on or above those lines, ``"<name> (line N)"`` for the declaration comment of a field the
    lines use."""
    repo = Path(repo)
    table = cached_items(repo) if table is None else table
    if not table:
        return []
    try:
        lines = (repo / file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    b = min(b or a, len(lines))
    out: list[dict] = []
    seen: set[str] = set()

    def add(rid: str, ln: int, via: str | None) -> None:
        if rid not in seen:
            seen.add(rid)
            out.append({"item": table[rid], "line": ln, "via": via})

    for i in range(a, b + 1):
        for rid, ln in _cited_above(lines, i, table, context if i == a else 0):
            add(rid, ln, None)
    # the fields the lines use, declared in the same file: their declaration's comments explain them too
    used = set(re.findall(r"\b([a-z_$][\w$]*)\b", "\n".join(lines[a - 1:b])))
    for k, line in enumerate(lines, 1):
        m = re.match(r"^\s*(?:(?:private|public|protected|static|final|volatile|transient)\s+)+[\w<>\[\],.? ]+?\s+"
                     r"([a-z_$][\w$]*)\s*(?:=|;)", line)
        if m and m.group(1) in used and not a <= k <= b:
            for rid, ln in _cited_above(lines, k, table, context):
                add(rid, ln, f"{m.group(1)} (line {k})")
    return out


_CACHE: dict[tuple, dict[str, Item]] = {}


def cached_items(repo: Path) -> dict[str, Item]:
    """:func:`items`, kept while the backlog files keep their size and time."""
    repo = Path(repo)
    key = [str(repo)]
    for f in backlog_files(repo):
        try:
            st = (repo / f).stat()
            key.append((f, st.st_size, st.st_mtime_ns))
        except OSError:
            key.append((f, None))
    k = tuple(key)
    if k not in _CACHE:
        if len(_CACHE) > 16:
            _CACHE.clear()
        _CACHE[k] = items(repo)
    return _CACHE[k]


def _line_text(repo: Path, f: str, ln: int) -> str:
    try:
        lines = (Path(repo) / f).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return lines[ln - 1].strip() if 0 < ln <= len(lines) else ""


def lookup(repo: Path, target: str, *, graph=None, stale=None, max_sites: int = 20) -> dict:
    """``verinoda backlog TARGET``: an item id -> the item and the code comments that cite it; ``file:LINE[-LINE]``
    or a symbol (resolved with :func:`verinoda.naming.resolve`) -> the items that explain those lines."""
    repo = Path(repo)
    table = cached_items(repo)
    files = backlog_files(repo)
    base = {"target": target, "backlog_files": files}
    if not files:
        return {**base, "status": "no_backlog", "note": "no backlog file (docs/BACKLOG.md, BACKLOG.md ...; or "
                "\"backlog\": {\"files\": [...]} in .verinoda/config.json)"}
    t = target.strip()
    if t in table:
        import subprocess

        try:
            listed = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True,
                                    timeout=60).stdout.split("\n")
        except (OSError, subprocess.SubprocessError):
            listed = []
        if not [f for f in listed if f]:
            listed = [p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()
                      and ".verinoda" not in p.parts and ".git" not in p.parts]
        at = sites(repo, [f for f in listed if f], table).get(t, [])
        it = table[t]
        return {**base, "status": "found", "kind": "item", "item": _item_dict(it),
                "sites": [{"at": f"{f}:{ln}", "text": _line_text(repo, f, ln)[:160]} for f, ln in at[:max_sites]],
                "sites_total": len(at)}
    m = __import__("re").match(r"^(.+?):(\d+)(?:-(\d+))?$", t)
    if m and (repo / m.group(1)).is_file():
        f, a = m.group(1).replace("\\", "/"), int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        where = f"{f}:{a}" + (f"-{b}" if b != a else "")
    else:
        if graph is None:
            return {**base, "status": "not_found", "note": f"{t!r} is not a backlog item, a file:line or an indexed "
                                                           "symbol (run `verinoda scan` for symbols)"}
        from verinoda import naming

        g = graph()
        r = naming.resolve(g, t, stale=stale() if callable(stale) else (stale or ()))
        if not (r.exact and r.node and g.span(r.node) and g.file(r.node)):
            return {**base, "status": "ambiguous" if r.status == naming.AMBIGUOUS else "not_found",
                    "note": r.note or f"{t!r} is not a backlog item, a file:line or a symbol",
                    "candidates": [{"label": g.label(c), "at": f"{g.file(c)}:{g.line(c)}"}
                                   for c in list(r.candidates)[:8]]}
        f, (a, b) = g.file(r.node), g.span(r.node)
        where = f"{f}:{a}-{b} ({g.label(r.node)})"
    found = items_for(repo, f, a, b, table=table)
    code = "\n".join(_line_text(repo, f, k) for k in range(a, min(b, a + 60) + 1))
    names = set(re.findall(r"[A-Za-z_$][\w$]{3,}", code))
    enclosing = _enclosing_method(repo, f, a)
    if enclosing:
        names.add(enclosing)
    return {**base, "status": "found", "kind": "lines", "at": where, "text": _line_text(repo, f, a)[:160],
            "items": [{**_item_dict(x["item"]), "cited_at": f"{f}:{x['line']}",
                       "comment": _line_text(repo, f, x["line"])[:200], **({"via": x["via"]} if x["via"] else {}),
                       "excerpt": excerpt(x["item"].text, names | ({x["via"].split(" ")[0]} if x["via"] else set()))}
                      for x in found]}


def excerpt(text: str, names: set[str], limit: int = 420) -> str:
    """The sentences of an item that name the code in question (``bodyEntity``, ``bodyRef``), else its start."""
    parts = [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[*A-Z`(\[])", text) if s.strip()]
    low = {n.lower() for n in names if len(n) >= 4}
    df = {n: sum(1 for s in parts if n in s.lower()) for n in low}
    # a name few sentences use says more than one every sentence repeats (a class name, a common type)
    score = {i: sum(1.0 / df[n] for n in low if df[n] and n in s.lower()) for i, s in enumerate(parts)}
    hits = sorted((i for i in score if score[i] > 0), key=lambda i: (-score[i], i))
    if not hits:
        return text[:limit] + ("..." if len(text) > limit else "")
    out = ""
    for i in sorted(hits[:3]):
        if len(out) + len(parts[i]) > limit and out:
            break
        out += (" ... " if out else "") + parts[i]
    return out[:limit] + ("..." if len(out) > limit else "")


def _enclosing_method(repo: Path, f: str, a: int) -> str | None:
    """The name of the method whose declaration is the nearest above line ``a`` (a line lookup's context)."""
    try:
        lines = (Path(repo) / f).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    rx = re.compile(r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|default|override|"
                    r"suspend|fun|def)\s+)+[\w<>\[\],.? ]*?([a-zA-Z_$][\w$]*)\s*\(")
    for k in range(min(a, len(lines)), max(0, a - 200), -1):
        m = rx.match(lines[k - 1])
        if m:
            return m.group(1)
    return None


def _item_dict(it: Item) -> dict:
    return {"id": it.id, "title": it.title, "at": it.at, "text": it.text[:1200] + ("..." if len(it.text) > 1200 else "")}


def render(res: dict) -> str:
    if res["status"] != "found":
        out = [f"{res['target']}: {res['status']}" + (f" - {res['note']}" if res.get("note") else "")]
        out += [f"  - {c['label']} ({c['at']})" for c in res.get("candidates") or []]
        return "\n".join(out)
    if res["kind"] == "item":
        it = res["item"]
        out = [f"{it['id']} {it['title']} ({it['at']})"]
        if it["text"] and it["text"] != it["title"]:
            out.append("  " + it["text"][:600] + ("..." if len(it["text"]) > 600 else ""))
        out.append(f"cited in code at {res['sites_total']} place(s)" + (":" if res["sites"] else ""))
        out += [f"  {s['at']}: {s['text']}" for s in res["sites"]]
        if res["sites_total"] > len(res["sites"]):
            out.append(f"  (+{res['sites_total'] - len(res['sites'])} more: --json)")
        return "\n".join(out)
    out = [f"{res['at']}: {res['text']}"]
    if not res["items"]:
        out.append("  no backlog item is cited on these lines, just above them, or at the fields they use")
    for x in res["items"]:
        via = f" (through {x['via']})" if x.get("via") else ""
        out.append(f"  {x['id']} {x['title']} ({x['at']}) - cited at {x['cited_at']}{via}: {x['comment']}")
        if x["text"] and x["text"] != x["title"]:
            out.append("    " + (x.get("excerpt") or x["text"][:420]))
    return "\n".join(out)
