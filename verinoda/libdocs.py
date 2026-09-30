"""Library docs of the installed version, for ``api NAME --docs``.

Two quotes, both read from files on disk and never from the network: the docstring of the definition as the
file the environment would import writes it, and the section of the distribution's README (the long
description in its ``*.dist-info/METADATA``) that names the target. Each quote is whole lines of the file, cited
as ``path:start-end`` exactly as far as it is shown, with the installed version. Nothing is imported or run.
What the docs say is the library authors' statement, not observed behaviour: the answer quotes, it does not
verify.
"""
from __future__ import annotations

import ast
import re
import textwrap
import warnings
from pathlib import Path

MAX_LINES = 40
MAX_CHARS = 4000
_UNDERLINE = re.compile(r"^([=\-~^*+#`'\".:_])\1{2,}[ \t]*$")
_MD_OPEN = re.compile(r"^ {0,3}(#{1,6})(?=[ \t]|$)")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
# a changelog appended to the README: its sections describe other versions, never the installed one's usage
_CHANGELOG = re.compile(r"(?i)^\W*(change ?log|changes|history|release notes|release history|what'?s new|news"
                        r"|v?\d+\.\d+(\.\d+)?\b)")
_NOT_PROSE = ("<", "[![", "![", ".. image", ".. |", "|", "[!")

Rows = list[tuple[int, str]]   # (1-based line of the file, its text)


def _file_lines(path: Path) -> list[str] | None:
    """The file's lines as the file has them: split at ``\\n`` only (``str.splitlines`` also splits at form
    feeds and Unicode separators, which would shift every later line number)."""
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _quote(rows: Rows) -> dict:
    """Whole lines of ``rows``, cut at :data:`MAX_LINES` lines or :data:`MAX_CHARS` characters; the range cited
    is the lines shown. The text is dedented (only whitespace common to every line is removed)."""
    shown: Rows = []
    total = 0
    for no, ln in rows:
        if len(shown) >= MAX_LINES or (shown and total + len(ln) + 1 > MAX_CHARS):
            break
        shown.append((no, ln[:MAX_CHARS]))
        total += len(ln) + 1
    cut = len(shown) < len(rows) or any(len(ln) > MAX_CHARS for _, ln in rows[:len(shown)])
    while len(shown) > 1 and not shown[-1][1].strip():   # a cut can end on a blank line: it is not quoted
        shown.pop()
    return {"start": shown[0][0], "end": shown[-1][0], "text": textwrap.dedent("\n".join(t for _, t in shown)),
            "truncated": cut}


# -- docstrings ------------------------------------------------------------------------------------------------

def _doc_node(node: ast.AST) -> ast.Expr | None:
    body = getattr(node, "body", None)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0]
    return None


_DEFS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _bodies(node: ast.AST):
    """Every statement list directly under ``node`` (a body, an ``else``, a handler, a ``finally``)."""
    for name in ("body", "orelse", "finalbody"):
        stmts = getattr(node, name, None)
        if isinstance(stmts, list) and stmts and isinstance(stmts[0], ast.stmt):
            yield stmts
    for h in getattr(node, "handlers", []) or []:
        yield h.body


def _defs(node: ast.AST):
    """The definitions of a module or class body, including those under ``if`` and ``try`` at that level."""
    for stmts in _bodies(node):
        for child in stmts:
            if isinstance(child, _DEFS):
                yield child
            elif isinstance(child, (ast.If, ast.Try)):
                yield from _defs(child)


def _node_at(tree: ast.Module, line: int) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, _DEFS) and (node.lineno == line or any(d.lineno == line for d in node.decorator_list)):
            return node
    return None


def _node_named(tree: ast.Module, parts: list[str]) -> ast.AST | None:
    node: ast.AST = tree
    for part in parts:
        node = next((d for d in _defs(node) if d.name == part), None)
        if node is None:
            return None
    return node


def _overload(node: ast.AST) -> bool:
    return any((d.id if isinstance(d, ast.Name) else d.attr if isinstance(d, ast.Attribute) else "") == "overload"
               for d in getattr(node, "decorator_list", []))


def _implementation(tree: ast.Module, stub: ast.AST) -> ast.AST | None:
    """The definition an ``@overload`` stub belongs to: a later one of the same name in the same statement list
    that is not an overload itself (where its docstring is written); None for overloads without one."""
    for node in ast.walk(tree):
        for stmts in _bodies(node):
            if any(s is stub for s in stmts):
                later = [s for s in stmts if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and s.name == stub.name and s.lineno > stub.lineno]
                return next((s for s in later if not _overload(s)), None)
    return None


def _binding_at(tree: ast.Module, line: int) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno <= line <= (node.end_lineno or line):
            return "a re-export (an import)"
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.lineno <= line <= (node.end_lineno or line):
            return "an alias or a value (an assignment)"
    return None


def _import_of(tree: ast.Module, name: str) -> tuple[str, int] | None:
    """``(module, level)`` of the module-level ``from module import name`` that binds ``name``."""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for al in node.names:
                if (al.asname or al.name) == name and al.name != "*":
                    return (node.module or "", node.level or 0)
    return None


def docstring(path: Path, line: int | None = None, names: list[str] | None = None) -> dict:
    """The docstring of the module in ``path`` (no ``line`` and no ``names``), of the class or function
    defined at ``line``, or of the one reached by the dotted ``names`` inside the module, quoted as its source
    lines (``start``, ``end``, ``text``, ``truncated``). Otherwise ``{"missing": why}``, and for a name the
    module only imports, ``"imported_from": (module, level)`` so the caller can follow it."""
    lines = _file_lines(path)
    if lines is None:
        return {"missing": "the file could not be read"}
    try:
        with warnings.catch_warnings():   # an installed file's invalid escapes are not the user's to see
            warnings.simplefilter("ignore")
            tree = ast.parse("\n".join(lines), filename=str(path))
    except (SyntaxError, ValueError, RecursionError):
        return {"missing": "the file could not be parsed as Python"}
    node: ast.AST | None = tree
    if line:
        node = _node_at(tree, line)
        if node is None:
            what = _binding_at(tree, line)
            return {"missing": f"line {line} is {what}, not a definition; its target was not followed" if what
                    else f"no class or function is defined at line {line}"}
    elif names:
        node = _node_named(tree, names)
        if node is None:
            out = {"missing": f"{'.'.join(names)} is not defined in this file (a re-export, or made at run time); "
                              "not followed"}
            imp = _import_of(tree, names[0])
            if imp is not None:
                out["imported_from"] = imp
            return out
    if _doc_node(node) is None and _overload(node):
        node = _implementation(tree, node) or node
    expr = _doc_node(node)
    if expr is None:
        return {"missing": "the definition has no docstring"}
    a, b = expr.lineno, expr.end_lineno or expr.lineno
    return _quote([(k, lines[k - 1]) for k in range(a, b + 1)])


# -- README sections --------------------------------------------------------------------------------------------

def _metadata(dist_info: Path) -> Path | None:
    for name in ("METADATA", "PKG-INFO"):
        if (dist_info / name).is_file():
            return dist_info / name
    return None


def long_description(meta: Path) -> Rows:
    """The README a distribution carries: the body after the metadata headers (Metadata 2.1+), else the
    indented continuation lines of an old ``Description:`` header (their 8-space or ``|`` prefix removed).
    Blank lines around it are dropped."""
    lines = _file_lines(meta) or []
    i, desc, in_desc = 0, [], False
    while i < len(lines) and lines[i] != "":    # the headers end at the first empty line
        ln = lines[i]
        if ln.startswith("Description:"):
            in_desc = True
            first = ln[len("Description:"):].strip()
            if first:
                desc.append((i + 1, first))
        elif in_desc and ln[:1] in (" ", "\t"):
            t = ln[8:] if ln.startswith(" " * 8) else ln.lstrip(" \t")
            desc.append((i + 1, t[1:] if t.startswith("|") else t))
        else:
            in_desc = False
        i += 1
    body = [(k + 1, lines[k]) for k in range(i + 1, len(lines))]
    rows = body if any(t.strip() for _, t in body) else desc
    while rows and not rows[0][1].strip():
        rows = rows[1:]
    while rows and not rows[-1][1].strip():
        rows = rows[:-1]
    return rows


def _md_title(ln: str) -> str | None:
    """The title of a Markdown ATX heading line, else None (read without a backtracking pattern)."""
    m = _MD_OPEN.match(ln)
    if m is None:
        return None
    t = ln[m.end():].strip()
    u = t.rstrip("#")
    if u != t and (u == "" or u[-1] in " \t"):   # a closing run of '#'
        t = u.rstrip()
    return t


def _scan(rows: Rows) -> tuple[list[tuple[int, str]], set[int]]:
    """(row index, title) of each Markdown or reStructuredText heading, and the row indexes of code lines
    (fenced, indented by four or more, a reStructuredText literal block after ``::``, ``>>>``). Lines in
    fenced code are never headings (a ``# comment`` in a shell example)."""
    heads: list[tuple[int, str]] = []
    code: set[int] = set()
    fence = None
    literal = False
    for i, (_, ln) in enumerate(rows):
        m = _FENCE.match(ln)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            code.add(i)
            continue
        if fence:
            code.add(i)
            continue
        if literal and ln.strip() and not ln[:1].isspace():
            literal = False
        if ln.strip() and (literal or ln.startswith(("    ", "\t")) or ln.lstrip().startswith(">>>")):
            code.add(i)
        if ln.rstrip().endswith("::"):
            literal = True
        md = _md_title(ln) if i not in code else None
        if md:
            heads.append((i, md))
            continue
        title = ln.strip()
        if title and i + 1 < len(rows) and _UNDERLINE.match(rows[i + 1][1]) and \
                len(rows[i + 1][1].strip()) >= len(title) and not _UNDERLINE.match(ln):
            over = i > 0 and _UNDERLINE.match(rows[i - 1][1])
            heads.append((i - 1 if over else i, title))
    return heads, code


def _sections(rows: Rows) -> tuple[list[tuple[int, int, str | None]], set[int]]:
    """``(first row, end row, heading)`` of each section before any changelog, and the code rows."""
    heads, code = _scan(rows)
    cut = next((i for i, t in heads if _CHANGELOG.match(t)), len(rows))
    heads = [h for h in heads if h[0] < cut]
    bounds = [(start, heads[k + 1][0] if k + 1 < len(heads) else cut, title)
              for k, (start, title) in enumerate(heads)]
    if not bounds or bounds[0][0] > 0:
        bounds.insert(0, (0, bounds[0][0] if bounds else cut, None))
    return [b for b in bounds if b[1] > b[0]], code


def _in_url(ln: str, a: int, b: int) -> bool:
    s = ln.rfind(" ", 0, a) + 1
    e = ln.find(" ", b)
    token = ln[s:e if e >= 0 else len(ln)]
    return "://" in token or "www." in token or ln[a - 1:a] == "/" or ln[b:b + 1] == "/"


def _section_rows(rows: Rows, start: int, end: int) -> Rows:
    out = rows[start:end]
    while len(out) > 1 and not out[-1][1].strip():
        out = out[:-1]
    return out


def _prose(ln: str) -> bool:
    s = ln.strip()
    return len(re.findall(r"[^\W\d_]{2,}", s)) >= 3 and not s.startswith(_NOT_PROSE)


def readme_section(dist_info: Path, names: list[str], *, top_level: bool = False) -> dict | None:
    """The section of the distribution's README that names the target. A top-level package's is the README's
    opening section (the first that has a line of prose: not only a logo or badges). Otherwise ``names`` are
    tried in order: a heading that names one wins, else the first section whose text names it. A dotted name
    counts anywhere outside a URL; a bare last part only where it reads as code (in backticks, after ``.``,
    before ``(``, on a code line), so ``Command`` is not found in "Command Line". A changelog appended to the
    README is not searched. None when nothing qualifies."""
    meta = _metadata(dist_info)
    if meta is None:
        return None
    rows = long_description(meta)
    if not rows:
        return None
    bounds, code = _sections(rows)
    hit = None
    matched = None
    if top_level:
        hit = next((b for b in bounds if any(_prose(t) for _, t in rows[b[0]:b[1]])), None)
    for k, name in enumerate([] if top_level else names):
        word = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")
        strict = k > 0 and "." not in name
        head_word = word if strict else re.compile(word.pattern, re.I)

        def names_it(i: int, word=word, strict=strict) -> bool:
            ln = rows[i][1]
            for mt in word.finditer(ln):
                if _in_url(ln, mt.start(), mt.end()):
                    continue
                if not strict or i in code or ln[:mt.start()].count("`") % 2 == 1 \
                        or ln[mt.start() - 1:mt.start()] == "." or ln[mt.end():mt.end() + 1] == "(":
                    return True
            return False

        hit = next((b for b in bounds if b[2] and head_word.search(b[2])), None) or \
            next((b for b in bounds if any(names_it(i) for i in range(b[0], b[1]))), None)
        if hit is not None:
            matched = name
            break
    if hit is None:
        return None
    q = _quote(_section_rows(rows, hit[0], hit[1]))
    return {"path": meta, **q, "heading": hit[2], "matched": matched}


def search_names(target: str) -> list[str]:
    """The names a README section is looked up by: the whole dotted target, then its last part."""
    parts = [p for p in target.replace(":", ".").split(".") if p]
    out = [".".join(parts)] if parts else []
    if len(parts) > 1:
        out.append(parts[-1])
    return out


NOTE = "quoted from the files as installed; the docs state the authors' intent, not verified behaviour"


def empty(read_from: str, *notes: str) -> dict:
    """A ``docs`` block with nothing to quote (the same keys as :func:`build`'s)."""
    return {"read_from": read_from, "quotes": [], "notes": list(notes), "note": NOTE}


def build(target: str, *, src_path: Path | None, src_line: int | None, src_names: list[str] | None,
          dist: tuple[str, str, Path] | None, disp, top_level: bool, origin: str = "not installed",
          doc: dict | None = None) -> dict:
    """The ``docs`` block of an ``api`` answer. ``disp`` turns a path into its display form; ``dist`` is
    ``(name, version, dist-info dir)`` of the installed distribution the definition comes from; ``origin``
    says where a definition without one comes from (the standard library, this project). ``doc``: the
    docstring already read (:func:`docstring`)."""
    out: dict = {"read_from": "installed files" if dist else "source files", "quotes": []}
    if dist:
        out["version"] = f"{dist[0]} {dist[1]}"
    notes: list[str] = []
    if src_path is not None and src_path.suffix in (".py", ".pyi"):
        doc = doc or docstring(src_path, src_line, src_names)
        if "missing" not in doc:
            out["quotes"].append({"kind": "docstring", "at": f"{disp(src_path)}:{doc['start']}-{doc['end']}",
                                  "text": doc["text"], "truncated": doc["truncated"]})
        else:
            notes.append(doc["missing"])
    elif src_path is not None:
        notes.append(f"{disp(src_path)} is compiled; no source docstring to quote")
    else:
        notes.append("no source file for the definition; no docstring to quote")
    if dist:
        names = search_names(target)
        sec = readme_section(dist[2], names, top_level=top_level)
        if sec:
            out["quotes"].append({"kind": "readme", "at": f"{disp(sec['path'])}:{sec['start']}-{sec['end']}",
                                  **({"heading": sec["heading"]} if sec["heading"] else {}),
                                  "text": sec["text"], "truncated": sec["truncated"]})
        elif top_level:
            notes.append(f"the {dist[0]} README (the METADATA long description) is empty or has no prose")
        else:
            notes.append(f"no section of the {dist[0]} README (the METADATA long description) names "
                         f"{' or '.join(names) or target}")
    else:
        notes.append(f"{origin}: no packaged README to quote")
    if notes:
        out["notes"] = notes
    out["note"] = NOTE
    return out
