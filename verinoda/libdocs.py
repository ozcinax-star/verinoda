"""Library docs of the installed version, for ``api NAME --docs``.

Two quotes, both read from files on disk and never from the network: the docstring of the definition as the
file the environment would import writes it, and the section of the distribution's README (the long
description in its ``*.dist-info/METADATA``) that names the target. Each quote carries its ``path:start-end``
and the installed version. Nothing is imported or run. What the docs say is the library authors' statement,
not observed behaviour: the answer quotes, it does not verify.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

MAX_LINES = 40
MAX_CHARS = 4000
_UNDERLINE = re.compile(r"^([=\-~^*+#`'\".:_])\1{2,}\s*$")
_MD_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, ValueError, RecursionError):
        return None


def _doc_node(node: ast.AST) -> ast.Expr | None:
    body = getattr(node, "body", None)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0]
    return None


def _defs(node: ast.AST):
    for child in getattr(node, "body", []):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child
        elif isinstance(child, (ast.If, ast.Try)):   # `if sys.version_info >= ...:` / `try: ... except ImportError`
            yield from _defs(child)
            for extra in (getattr(child, "orelse", []), *(h.body for h in getattr(child, "handlers", []))):
                yield from _defs(ast.Module(body=extra, type_ignores=[]))


def _node_at(tree: ast.Module, line: int) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and \
                (node.lineno == line or any(d.lineno == line for d in node.decorator_list)):
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
    """The definition an ``@overload`` stub belongs to: the next one of the same name and indentation that is
    not an overload itself (where its docstring is written)."""
    later = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == stub.name and n.col_offset == stub.col_offset and n.lineno > stub.lineno]
    return next((n for n in sorted(later, key=lambda n: n.lineno) if not _overload(n)), None)


def _cap(lines: list[str]) -> tuple[str, bool]:
    cut = len(lines) > MAX_LINES
    text = "\n".join(lines[:MAX_LINES])
    if len(text) > MAX_CHARS:
        text, cut = text[:MAX_CHARS], True
    return text, cut


def docstring(path: Path, line: int | None = None, names: list[str] | None = None) -> dict | None:
    """The docstring of the module in ``path`` (no ``line`` and no ``names``), of the class or function
    defined at ``line``, or of the one reached by the dotted ``names`` inside the module; None when the
    file does not parse or the definition has no docstring."""
    tree = _parse(path)
    if tree is None:
        return None
    node: ast.AST | None = tree
    if line:
        node = _node_at(tree, line)
    elif names:
        node = _node_named(tree, names)
    if node is not None and _doc_node(node) is None and _overload(node):
        node = _implementation(tree, node) or node
    expr = _doc_node(node) if node is not None else None
    if expr is None:
        return None
    text, cut = _cap(inspect.cleandoc(expr.value.value).splitlines())
    return {"start": expr.lineno, "end": expr.end_lineno or expr.lineno, "text": text, "truncated": cut}


def _metadata(dist_info: Path) -> Path | None:
    for name in ("METADATA", "PKG-INFO"):
        if (dist_info / name).is_file():
            return dist_info / name
    return None


def _headings(lines: list[str], first: int, code: set[int] | None = None) -> list[tuple[int, str]]:
    """(0-based index, title) of each Markdown or reStructuredText heading from ``first`` on; lines inside
    fenced code blocks are not headings (a ``# comment`` in a shell example). ``code`` collects the indexes
    of code lines: fenced, indented by four or more, a reStructuredText literal block after ``::``, ``>>>``."""
    out: list[tuple[int, str]] = []
    fence = None
    literal = False
    for i in range(first, len(lines)):
        ln = lines[i]
        m = _FENCE.match(ln)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence:
            if code is not None:
                code.add(i)
            continue
        if literal and ln.strip() and not ln[:1].isspace():
            literal = False
        if code is not None and ln.strip() and \
                (literal or ln.startswith(("    ", "\t")) or ln.lstrip().startswith(">>>")):
            code.add(i)
        if ln.rstrip().endswith("::"):
            literal = True
        md = _MD_HEADING.match(ln)
        if md:
            out.append((i, md.group(2)))
            continue
        title = ln.strip()
        if title and i + 1 < len(lines) and _UNDERLINE.match(lines[i + 1]) and \
                len(lines[i + 1].strip()) >= len(title) and not _UNDERLINE.match(ln):
            if i > first and _UNDERLINE.match(lines[i - 1]):   # an overlined title: the section starts above
                out.append((i - 1, title))
            else:
                out.append((i, title))
    return out


def readme_section(dist_info: Path, names: list[str]) -> dict | None:
    """The section of the distribution's README (its METADATA long description) whose heading names one of
    ``names`` (tried in order), else the first section whose text names it. A README without headings is
    one section. None when nothing names the target or there is no long description."""
    meta = _metadata(dist_info)
    if meta is None:
        return None
    try:
        lines = meta.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    # the headers end at the first blank line; the rest is the long description (Metadata 2.1+)
    body = next((i + 1 for i, ln in enumerate(lines) if not ln.strip()), len(lines))
    while body < len(lines) and not lines[body].strip():
        body += 1
    if body >= len(lines):
        return None
    code: set[int] = set()
    heads = _headings(lines, body, code)
    bounds = [(start, heads[k + 1][0] if k + 1 < len(heads) else len(lines), title)
              for k, (start, title) in enumerate(heads)]
    if not bounds or bounds[0][0] > body:
        bounds.insert(0, (body, bounds[0][0] if bounds else len(lines), None))
    for k, name in enumerate(names):
        word = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")   # `x.Widget()` names Widget
        # a dotted name names the target anywhere; a bare last part in running text only where it reads as
        # code (the class Command, not the words "Command Line")
        strict = k > 0 and "." not in name

        def names_it(i: int, word=word, strict=strict) -> bool:
            ln = lines[i]
            return any(not strict or i in code or ln[:mt.start()].count("`") % 2 == 1
                       or ln[mt.start() - 1:mt.start()] == "." or ln[mt.end():mt.end() + 1] == "("
                       for mt in word.finditer(ln))

        hit = next((b for b in bounds if b[2] and word.search(b[2])), None) or \
            next((b for b in bounds if any(names_it(i) for i in range(b[0], b[1]))), None)
        if hit is None:
            continue
        start, end = hit[0], hit[1]
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        text, cut = _cap(lines[start:end])
        shown_end = start + min(end - start, MAX_LINES)
        return {"path": meta, "start": start + 1, "end": shown_end, "heading": hit[2], "matched": name,
                "text": text, "truncated": cut}
    return None


def search_names(target: str) -> list[str]:
    """The names a README section is looked up by: the whole dotted target, then its last part."""
    parts = [p for p in target.replace(":", ".").split(".") if p]
    out = [".".join(parts)] if parts else []
    if len(parts) > 1:
        out.append(parts[-1])
    return out


def build(target: str, *, src_path: Path | None, src_line: int | None, src_names: list[str] | None,
          dist: tuple[str, str, Path] | None, disp, top_level: bool, origin: str = "not installed") -> dict:
    """The ``docs`` block of an ``api`` answer. ``disp`` turns a path into its display form; ``dist`` is
    ``(name, version, dist-info dir)`` of the installed distribution the definition comes from; ``origin``
    says where a definition without one comes from (the standard library, this project)."""
    out: dict = {"read_from": "installed files" if dist else "source files", "quotes": []}
    if dist:
        out["version"] = f"{dist[0]} {dist[1]}"
    notes: list[str] = []
    if src_path is not None and src_path.suffix in (".py", ".pyi"):
        doc = docstring(src_path, src_line, src_names)
        if doc:
            out["quotes"].append({"kind": "docstring", "at": f"{disp(src_path)}:{doc['start']}-{doc['end']}",
                                  "text": doc["text"], "truncated": doc["truncated"]})
        else:
            notes.append("the definition has no docstring")
    elif src_path is not None:
        notes.append(f"{disp(src_path)} is compiled; no source docstring to quote")
    else:
        notes.append("no source file for the definition; no docstring to quote")
    if dist:
        names = search_names(target)
        # a top-level package is what the whole README is about: its opening section when no heading names it
        sec = readme_section(dist[2], names) or (_intro(dist[2]) if top_level else None)
        if sec:
            out["quotes"].append({"kind": "readme", "at": f"{disp(sec['path'])}:{sec['start']}-{sec['end']}",
                                  **({"heading": sec["heading"]} if sec["heading"] else {}),
                                  "text": sec["text"], "truncated": sec["truncated"]})
        else:
            notes.append(f"no section of the {dist[0]} README (the METADATA long description) names "
                         f"{' or '.join(names) or target}")
    else:
        notes.append(f"{origin}: no packaged README to quote")
    if notes:
        out["notes"] = notes
    out["note"] = "quoted from the files as installed; the docs state the authors' intent, not verified behaviour"
    return out


def _intro(dist_info: Path) -> dict | None:
    """The README's first section: what a top-level package target quotes when no section names it."""
    meta = _metadata(dist_info)
    if meta is None:
        return None
    try:
        lines = meta.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    body = next((i + 1 for i, ln in enumerate(lines) if not ln.strip()), len(lines))
    while body < len(lines) and not lines[body].strip():
        body += 1
    if body >= len(lines):
        return None
    heads = [h for h in _headings(lines, body) if h[0] > body]
    end = heads[0][0] if heads else len(lines)
    while end > body + 1 and not lines[end - 1].strip():
        end -= 1
    text, cut = _cap(lines[body:end])
    return {"path": meta, "start": body + 1, "end": body + min(end - body, MAX_LINES), "heading": None,
            "text": text, "truncated": cut}
