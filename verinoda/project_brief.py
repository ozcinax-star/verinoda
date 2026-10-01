"""The project brief (``verinoda brief``): a short summary of how the project is built, tested and laid out.

Agents keep a small "memory block" of project facts (the name, how to build and test it, where the code is, the
conventions). Written by hand it goes stale. :func:`brief` builds it again on every call from the files as they
are now, and every line names where it was read (``file:line``, or the folder for a layout count):

- project: the ``pyproject.toml`` / root ``package.json`` name, version and runtime (``requires-python``,
  ``engines.node``), and the console scripts and ``bin`` entries;
- build, test, check: ``package.json`` scripts, Makefile targets and justfile recipes whose name says test, lint,
  format, typecheck or build (:mod:`verinoda.agentlint`'s role names), the build backend, the pytest settings;
- ci: the commands ``.github/workflows/*.yml`` (``run:``) and ``.gitlab-ci.yml`` (``script:``) run;
- layout: the top-level folders by number of files (git's file list), each with its main extensions;
- conventions: ``.editorconfig`` ``[*]``, ``.gitattributes`` rules, line-length and strictness settings, the
  pre-commit hooks, the agent instruction files.

Every line states what a file says (``statically_verified``); nothing is guessed from names alone except the
section a script or target is listed under. The text is cut to a character budget: the first two lines of each
section, then the rest, in ``PRIORITY`` order (CI last), stopping at the first line that does not fit; what did
not fit, and the CI commands past ``MAX_CI``, are counted. Not the decision brief (``verinoda decide brief``).
Read-only: nothing runs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from verinoda.agentlint import TOOL_ROLES, Tree, _json, _key_rx, _line_of, _name_role, _read

VERIFIED = "statically_verified"
DEFAULT_CHARS = 2000
MIN_CHARS, MAX_CHARS = 200, 20000
MAX_CMD = 100        # characters of one command or script body shown
MAX_CI = 10          # distinct CI commands
MAX_DIRS = 8         # top-level folders in the layout
MAX_HOOKS = 8        # pre-commit hook ids named
SECTIONS = ("project", "build", "test", "check", "ci", "layout", "conventions")   # as printed
PRIORITY = ("project", "build", "test", "check", "layout", "conventions", "ci")   # what the budget keeps first
_SECTION_OF_ROLE = {"build": "build", "test": "test", "lint": "check", "format": "check", "typecheck": "check"}
_INSTRUCTIONS = ("AGENTS.md", "AGENT.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md",
                 ".cursorrules", ".windsurfrules", ".clinerules")
_PRE_COMMIT = ".pre-commit-config.yaml"
# the indentation up to the key's own column (a step's ``- `` included), so a block is what is indented past it
_RUN = re.compile(r"^(\s*(?:-\s+)?)run:\s*(.*)$")
_SCRIPT = re.compile(r"^(\s*(?:-\s+)?)(?:before_script|script|after_script):\s*(.*)$")
_BLOCK = re.compile(r"^([|>])[+-]?\d*\s*(#.*)?$")
# a TOML / INI table header: ``[a.b]`` or ``[[a.b]]``, a comment after it allowed
_TABLE = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(?:[#;].*)?$")
_KEY = re.compile(r"^\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s=#;\[\"']+))\s*=")
# shell plumbing a CI step runs around the commands (not how the project is built or tested), and a value that
# is only a GitHub expression
_CI_SKIP = re.compile(r"^(?:(?:echo|cd|set|export|if|fi|then|else|done|do|for|mkdir|printf|cat|ls|git config)\b"
                      r"|\$\{\{)")
_INSTALL = re.compile(r"\b(?:pip|uv pip|uv add|npm|pnpm|yarn|bun)\s+(?:install|add|i|ci)\b")
_HOOK_ID = re.compile(r"^\s*-\s*id:\s*([\w.-]+)")


def _short(s: str, n: int = MAX_CMD) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 3] + "..."


def _line(section: str, text: str, *at: str, status: str = VERIFIED) -> dict:
    return {"section": section, "text": text, "status": status, "evidence": list(at)}


def _dict(v) -> dict:
    """``v`` when it is a table / object, else empty (a manifest of an unexpected shape declares nothing)."""
    return v if isinstance(v, dict) else {}


def _lines(t: Tree, rel: str) -> list[str]:
    return t.memo(f"brief-lines:{rel}", lambda: (_read(t.repo / rel) or "").splitlines())


def _at_file(t: Tree, rel: str) -> str:
    """``rel:1`` for a file that has a first line; the bare path for an empty one (no line to cite)."""
    return f"{rel}:1" if _lines(t, rel) else rel


def _keys(t: Tree, rel: str) -> dict[tuple[str, str | None], int]:
    """TOML / INI ``(table, key)`` -> the line that sets it, and ``(table, None)`` -> the table's header line;
    the first one wins. Read once per file."""
    def make():
        out: dict[tuple[str, str | None], int] = {}
        table = ""
        for i, ln in enumerate(_lines(t, rel), 1):
            m = _TABLE.match(ln)
            if m:
                table = m.group(1)
                out.setdefault((table, None), i)
                continue
            k = _KEY.match(ln)
            if k:
                out.setdefault((table, next(g for g in k.groups() if g is not None)), i)
        return out
    return t.memo(f"brief-keys:{rel}", make)


def _key_line(t: Tree, rel: str, table: str, key: str) -> int | None:
    """The line of ``key =`` inside TOML/INI table ``[table]`` (None when the table does not set it)."""
    return _keys(t, rel).get((table, key))


def _table_line(t: Tree, rel: str, table: str) -> int | None:
    return _keys(t, rel).get((table, None))


def _toml_at(t: Tree, table: str, key: str) -> str:
    """``pyproject.toml:N`` for ``key`` of ``[table]``; a key written another way (a dotted key, an inline
    table) is found by its name."""
    pp = t.repo / "pyproject.toml"
    return f"pyproject.toml:{_key_line(t, 'pyproject.toml', table, key) or _line_of(pp, _key_rx(key.split('.')[-1]))}"


def _strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (tsconfig's JSONC) as plain JSON; strings are kept as they are."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == ",":
            j = i + 1
            while j < n:   # the next thing that is not blank or a comment
                if text[j].isspace():
                    j += 1
                elif text.startswith("//", j):
                    k = text.find("\n", j)
                    j = n if k < 0 else k
                elif text.startswith("/*", j):
                    k = text.find("*/", j + 2)
                    j = n if k < 0 else k + 2
                else:
                    break
            if j >= n or text[j] not in "}]":   # a trailing comma is dropped
                out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _jsonc(t: Tree, rel: str) -> dict:
    p = t.repo / rel
    data = _json(p)
    if data:
        return data
    try:
        data = json.loads(_strip_jsonc(_read(p) or "{}"))
    except ValueError:
        return {}
    return _dict(data)


def _json_keys(t: Tree, rel: str) -> dict[tuple[str, ...], int]:
    """Every key of a JSON (or JSONC) file, by its path of object keys from the root -> the line it is on (the
    first one wins): the line of ``name`` is the top-level one, not ``author.name``."""
    def make():
        text = _read(t.repo / rel) or ""
        out: dict[tuple[str, ...], int] = {}
        stack: list[list] = []          # [bracket, the current key of an object]
        pending: tuple[str, int] | None = None
        i, n, line = 0, len(text), 1
        while i < n:
            c = text[i]
            if c == '"':
                j = i + 1
                while j < n and text[j] not in '"\n':
                    j += 2 if text[j] == "\\" else 1
                pending = (text[i + 1:j], line)
                i = j + 1
                continue
            if text.startswith("//", i) or text.startswith("/*", i):
                j = text.find("\n" if text[i + 1] == "/" else "*/", i + 2)
                j = n if j < 0 else j + (0 if text[i + 1] == "/" else 2)
                line += text.count("\n", i, j)
                i = j
                continue
            if c == "\n":
                line += 1
            elif c == ":" and pending and stack and stack[-1][0] == "{":
                stack[-1][1] = pending[0]
                path = tuple(k for b, k in stack if b == "{" and k is not None)
                out.setdefault(path, pending[1])
            elif c in "{[":
                stack.append([c, None])
            elif c in "}]":
                if stack:
                    stack.pop()
            elif c == "," and stack and stack[-1][0] == "{":
                stack[-1][1] = None
            if not c.isspace():
                pending = None
            i += 1
        return out
    return t.memo(f"brief-json:{rel}", make)


def _json_at(t: Tree, rel: str, *path: str) -> str:
    return f"{rel}:{_json_keys(t, rel).get(path) or 1}"


# -- the sections ------------------------------------------------------------------------------------------------

def _project(t: Tree) -> list[dict]:
    out = []
    proj = _dict(t.pyproject().get("project"))
    if proj.get("name"):
        bits = [str(proj["name"])] + ([str(proj["version"])] if proj.get("version") else [])
        if proj.get("requires-python"):
            bits.append(f"Python {proj['requires-python']}")
        out.append(_line("project", "Python project " + ", ".join(bits), _toml_at(t, "project", "name")))
    for tbl, what in (("scripts", "console script"), ("gui-scripts", "GUI script")):
        for name, target in _dict(proj.get(tbl)).items():
            out.append(_line("project", f"{what} `{name}` -> {target}", _toml_at(t, f"project.{tbl}", name)))
    pj = t.repo / "package.json"
    data = _json(pj) if pj.is_file() else {}
    if data.get("name"):
        bits = [str(data["name"])] + ([str(data["version"])] if data.get("version") else [])
        node = _dict(data.get("engines")).get("node")
        if node:
            bits.append(f"Node {node}")
        out.append(_line("project", "JavaScript package " + ", ".join(bits), _json_at(t, "package.json", "name")))
    if isinstance(data.get("bin"), dict):
        for name, target in data["bin"].items():
            out.append(_line("project", f"bin `{name}` -> {target}", _json_at(t, "package.json", "bin", name)))
    elif isinstance(data.get("bin"), str):
        out.append(_line("project", f"bin -> {data['bin']}", _json_at(t, "package.json", "bin")))
    return out


def _commands(t: Tree) -> list[dict]:
    """Scripts, targets and recipes whose name gives a role, the build backend and the pytest settings."""
    out = []
    backend = _dict(t.pyproject().get("build-system")).get("build-backend")
    if backend:
        out.append(_line("build", f"build backend {backend}", _toml_at(t, "build-system", "build-backend")))
    if (t.repo / "package.json").is_file():
        mgr = next(iter(m for m in ("pnpm", "yarn", "bun", "npm") if m in t.locks()), "npm")
        for name, (body, ln) in t.scripts("package.json").items():
            role = _name_role(name)
            if role:
                out.append(_line(_SECTION_OF_ROLE[role], f"`{mgr} run {name}`: {_short(body)}", f"package.json:{ln}"))
    for mk in ("Makefile", "GNUmakefile", "makefile"):
        if (t.repo / mk).is_file():
            targets, _open = t.make_targets(mk)
            for name, (ln, recipe) in targets.items():
                role = _name_role(name)
                if role:
                    out.append(_line(_SECTION_OF_ROLE[role], f"`make {name}`" + (f": {_short(recipe)}" if recipe
                                                                                  else ""), f"{mk}:{ln}"))
            break
    for jf in ("justfile", "Justfile"):
        if (t.repo / jf).is_file():
            recipes, _open = t.just_recipes(jf)
            for name, ln in recipes.items():
                role = _name_role(name)
                if role:
                    out.append(_line(_SECTION_OF_ROLE[role], f"`just {name}`", f"{jf}:{ln}"))
            break
    out += _pytest(t)
    return out


def _pytest(t: Tree) -> list[dict]:
    for rel, table in (("pyproject.toml", "tool.pytest.ini_options"), ("pytest.ini", "pytest"),
                       ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        if not (t.repo / rel).is_file():
            continue
        start = _table_line(t, rel, table)
        if start is None:
            continue
        bits = []
        for key in ("testpaths", "addopts"):
            ln = _key_line(t, rel, table, key)
            if ln:
                bits.append(_short(_lines(t, rel)[ln - 1].strip(), 60))
        return [_line("test", "pytest settings" + (": " + "; ".join(bits) if bits else ""), f"{rel}:{start}")]
    return []


def _ci_files(t: Tree) -> list[str]:
    return sorted(f for f in t.files if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", f)) + \
        [f for f in t.files if f == ".gitlab-ci.yml"]


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip())


def _unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"" else s


def _flow(val: str) -> list[str]:
    """The items of a YAML flow list ``["a", 'b', c]``."""
    try:
        items = json.loads(val)
        if isinstance(items, list):
            return [str(x) for x in items]
    except ValueError:
        pass
    return [_unquote(x) for x in re.findall(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^,]+", val[1:-1]) if x.strip()]


def _block(lines: list[str], i: int, ind: int, style: str) -> tuple[list[tuple[int, str]], int]:
    """The commands of a block scalar (the lines indented past column ``ind`` from index ``i``), each with its
    first line: a ``>`` block folds a paragraph into one command; in a ``|`` block a line ending with a
    backslash goes on in the next one. Also the index after the block."""
    cmds: list[tuple[int, str]] = []
    cur: list | None = None
    while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > ind):
        s = lines[i].strip()
        i += 1
        if not s:
            if cur and style == ">":
                cmds.append((cur[0], cur[1]))
                cur = None
            continue
        if cur is None:
            if s.startswith("#"):
                continue
            cur = [i, ""]
        more = s.endswith("\\")
        cur[1] = (cur[1] + " " + (s[:-1] if more else s)).strip()
        if style != ">" and not more:
            cmds.append((cur[0], cur[1]))
            cur = None
    if cur:
        cmds.append((cur[0], cur[1]))
    return cmds, i


def _items(lines: list[str], i: int, ind: int) -> tuple[list[tuple[int, str]], int]:
    """The items of a YAML block list under a ``script:`` key at column ``ind`` (items may sit at the key's own
    column): a plain or quoted item, its continuation lines folded in, or a ``|`` / ``>`` block."""
    cmds: list[tuple[int, str]] = []
    while i < len(lines):
        raw, s = lines[i], lines[i].strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        d = _indent(raw)
        if d < ind or (d == ind and not s.startswith("- ")):
            break
        i += 1
        if not s.startswith("- "):
            continue
        val, ln = s[2:].strip(), i
        b = _BLOCK.match(val)
        if b:
            sub, i = _block(lines, i, d, b.group(1))
            cmds += sub
            continue
        while i < len(lines) and lines[i].strip() and _indent(lines[i]) > d and \
                not lines[i].strip().startswith(("- ", "#")):
            val += " " + lines[i].strip()
            i += 1
        cmds.append((ln, _unquote(val)))
    return cmds, i


def _ci(t: Tree) -> list[dict]:
    """The commands CI runs: ``run:`` values and ``script:`` items (one line, a ``|`` / ``>`` block, a block or
    flow list), test commands first."""
    seen: dict[str, dict] = {}
    for rel in _ci_files(t):
        gitlab = rel.endswith(".gitlab-ci.yml")
        rx = _SCRIPT if gitlab else _RUN
        lines = _lines(t, rel)
        i = 0
        while i < len(lines):
            m = rx.match(lines[i])
            i += 1
            if not m:
                continue
            ind, val = len(m.group(1)), m.group(2).strip()
            b = _BLOCK.match(val)
            if b:
                cmds, i = _block(lines, i, ind, b.group(1))
            elif val.startswith("[") and val.endswith("]"):
                cmds = [(i, c) for c in _flow(val)]
            elif val and not val.startswith("#"):
                cmds = [(i, _unquote(val))]
            elif gitlab:
                cmds, i = _items(lines, i, ind)
            else:   # a plain scalar on the next lines
                cmds, i = _block(lines, i, ind, ">")
            for ln, cmd in cmds:
                cmd = cmd.strip()
                if cmd and not _CI_SKIP.match(cmd):
                    seen.setdefault(_short(cmd), _line("ci", f"`{_short(cmd)}`", f"{rel}:{ln}"))
    rows = list(seen.values())
    # the test commands first: a brief with little room keeps what CI checks a change with
    rows.sort(key=lambda r: 0 if _runs_test(r["text"].strip("`")) else 1)
    return rows


def _runs_test(cmd: str) -> bool:
    """The command runs a test tool (``pytest``, ``python -m pytest``, ``npx jest``, ``npm test``, ``make
    test``), not one that installs it."""
    if _INSTALL.search(cmd):
        return False
    tools = "|".join(re.escape(k) for k, v in TOOL_ROLES.items() if v == "test")
    return bool(re.search(rf"(?:^|[\s/]|-m\s+)(?:{tools})(?:\s|$)", cmd) or
                re.search(r"\b(?:npm|pnpm|yarn|bun|make|just)\s+(?:run\s+)?test\b", cmd))


def _layout(t: Tree) -> list[dict]:
    """Top-level folders; a count comes from git's file list, so its evidence is the folder, after the manifest
    line that says what the folder is (if one does)."""
    per: dict[str, list[str]] = {}
    for f in t.files:
        parts = PurePosixPath(f).parts
        if len(parts) > 1:
            per.setdefault(parts[0], []).append(f)
    roles = _folder_roles(t)
    rank = {d: i for i, d in enumerate(roles)}   # in the order _folder_roles found them
    ranked = sorted(per.items(), key=lambda kv: (rank.get(kv[0], len(rank)), -len(kv[1]), kv[0]))
    out = []
    for d, fs in ranked[:MAX_DIRS]:
        exts: dict[str, int] = {}
        for f in fs:
            ext = PurePosixPath(f).suffix.lower() or PurePosixPath(f).name
            exts[ext] = exts.get(ext, 0) + 1
        main = ", ".join(f"{e} {n}" for e, n in sorted(exts.items(), key=lambda kv: (-kv[1], kv[0]))[:3])
        role, at = roles.get(d, ("", None))
        out.append(_line("layout", f"{d}/: {role + ', ' if role else ''}{len(fs)} files ({main})",
                         *([at] if at else []), f"{d}/"))
    if len(per) > MAX_DIRS:
        rest = ranked[MAX_DIRS:]
        out.append(_line("layout", f"{len(rest)} more folders, {sum(len(v) for _, v in rest)} files",
                         *[f"{d}/" for d, _ in rest[:3]]))
    return out


def _folder_roles(t: Tree) -> dict[str, tuple[str, str]]:
    """Top-level folder -> (what a file says it is, where), in this order: the package a console script runs,
    pytest's ``testpaths``, a folder with an ``__init__.py`` (the file itself is the evidence)."""
    out: dict[str, tuple[str, str]] = {}
    data = t.pyproject()
    for name, target in _dict(_dict(data.get("project")).get("scripts")).items():
        top = str(target).split(":")[0].split(".")[0]
        if top and f"{top}/__init__.py" in t.files:
            out.setdefault(top, (f"package of console script `{name}`", _toml_at(t, "project.scripts", name)))
    ini = _dict(_dict(_dict(data.get("tool")).get("pytest")).get("ini_options"))
    paths = ini.get("testpaths") or []
    for p in [paths] if isinstance(paths, str) else paths if isinstance(paths, list) else []:
        top = PurePosixPath(str(p)).parts[0] if str(p).strip("./") else ""
        if top:
            out.setdefault(top, ("pytest testpaths", _toml_at(t, "tool.pytest.ini_options", "testpaths")))
    for f in t.files:
        parts = PurePosixPath(f).parts
        if len(parts) == 2 and parts[1] == "__init__.py":
            out.setdefault(parts[0], ("Python package", f))
    return out


def _conventions(t: Tree) -> list[dict]:
    out = []
    if (t.repo / ".editorconfig").is_file():
        start = _table_line(t, ".editorconfig", "*")
        if start:
            keys = [f"{k}={v}" for k in ("indent_style", "indent_size", "end_of_line", "charset")
                    for ln in [_key_line(t, ".editorconfig", "*", k)] if ln
                    for v in [_lines(t, ".editorconfig")[ln - 1].split("=", 1)[1].strip()]]
            if keys:
                out.append(_line("conventions", ".editorconfig [*]: " + ", ".join(keys), f".editorconfig:{start}"))
    if (t.repo / ".gitattributes").is_file():
        for i, ln in enumerate(_lines(t, ".gitattributes"), 1):
            s = ln.strip()
            if s and not s.startswith("#") and re.search(r"(?:^|\s)(?:-?text\b|eol=|text=auto)", s):
                out.append(_line("conventions", f".gitattributes `{_short(s, 60)}`", f".gitattributes:{i}"))
                if sum(1 for r in out if r["text"].startswith(".gitattributes")) >= 2:
                    break
    tool = _dict(t.pyproject().get("tool"))
    for name in ("ruff", "black", "isort", "flake8"):
        ll = _dict(tool.get(name)).get("line-length")
        if ll:
            out.append(_line("conventions", f"{name} line-length {ll}", _toml_at(t, f"tool.{name}", "line-length")))
    if _dict(tool.get("mypy")).get("strict") is True:
        out.append(_line("conventions", "mypy strict", _toml_at(t, "tool.mypy", "strict")))
    if (t.repo / "tsconfig.json").is_file():
        opts = _jsonc(t, "tsconfig.json").get("compilerOptions")
        if isinstance(opts, dict) and "strict" in opts:
            out.append(_line("conventions", f"TypeScript strict: {str(opts['strict']).lower()}",
                             _json_at(t, "tsconfig.json", "compilerOptions", "strict")))
    if (t.repo / _PRE_COMMIT).is_file():
        hooks: dict[str, int] = {}
        for i, ln in enumerate(_lines(t, _PRE_COMMIT), 1):
            m = _HOOK_ID.match(ln)
            if m:
                hooks.setdefault(m.group(1), i)
        if hooks:
            shown = list(hooks.items())[:MAX_HOOKS]
            more = f" (+{len(hooks) - len(shown)} more)" if len(hooks) > len(shown) else ""
            out.append(_line("conventions", "pre-commit hooks: " + ", ".join(h for h, _ in shown) + more,
                             *[f"{_PRE_COMMIT}:{ln}" for _, ln in shown]))
    for rel in _INSTRUCTIONS:
        if rel in t.files:
            out.append(_line("conventions", f"agent instructions in {rel} (checked by `verinoda agent-lint`)",
                             _at_file(t, rel)))
    return out


# -- the budget --------------------------------------------------------------------------------------------------

def _cite(evidence: list[str]) -> str:
    """``a:3, 7, b/``: the lines of one file after its first citation are given by number only."""
    out: list[str] = []
    last = None
    for e in evidence:
        rel, _, ln = e.rpartition(":")
        if rel and ln.isdigit() and rel == last:
            out.append(ln)
        else:
            out.append(e)
            last = rel if rel and ln.isdigit() else None
    return ", ".join(out)


def _row(r: dict) -> str:
    mark = "" if r["status"] == VERIFIED else f" [{r['status']}]"
    return f"- {r['text']} ({_cite(r['evidence'])}){mark}"


def render_text(rows: list[dict], *, budget: int | None = None, omitted: int = 0, ci_cut: int = 0) -> str:
    out = ["# Project brief (from the files now; each line cites where it was read)"]
    for sec in SECTIONS:
        mine = [r for r in rows if r["section"] == sec]
        if mine:
            out.append(f"{sec}:")
            out += [_row(r) for r in mine]
    if omitted:
        out.append(f"({omitted} more line(s) over the {budget}-character budget: --max-chars)")
    if ci_cut:
        out.append(f"({ci_cut} more CI command(s) past the first {MAX_CI})")
    return "\n".join(out)


def _fit(rows: list[dict], budget: int, ci_cut: int = 0) -> tuple[list[dict], int]:
    """The rows that fit the budget: the first two of each section, then the rest, each in ``PRIORITY`` order."""
    count: dict[str, int] = {}
    first = []
    for r in rows:
        first.append(count.get(r["section"], 0) < 2)
        count[r["section"]] = count.get(r["section"], 0) + 1
    order = sorted(range(len(rows)), key=lambda i: (0 if first[i] else 1, PRIORITY.index(rows[i]["section"]), i))
    keep: set[int] = set()
    for i in order:
        trial = keep | {i}
        chosen = [rows[j] for j in sorted(trial)]
        left = len(rows) - len(trial)
        if len(render_text(chosen, budget=budget, omitted=left, ci_cut=ci_cut)) > budget:
            break   # a shorter line further down does not take the place of this one
        keep = trial
    return [rows[j] for j in sorted(keep)], len(rows) - len(keep)


def brief(repo: Path, *, max_chars: int = DEFAULT_CHARS, files: list[str] | None = None) -> dict:
    """``verinoda brief``: the project brief under ``max_chars`` characters, every line with evidence."""
    from verinoda.snapshot import list_files

    repo = Path(repo).resolve()
    budget = max(MIN_CHARS, min(MAX_CHARS, int(max_chars)))
    t = Tree(repo, list_files(repo) if files is None else files, Path.home())
    ci = _ci(t)
    ci_cut = max(0, len(ci) - MAX_CI)
    rows = _project(t) + _commands(t) + ci[:MAX_CI] + _layout(t) + _conventions(t)
    rows.sort(key=lambda r: SECTIONS.index(r["section"]))   # stable: each section keeps its own order
    kept, over = _fit(rows, budget, ci_cut)
    text = render_text(kept, budget=budget, omitted=over, ci_cut=ci_cut)
    omitted = over + ci_cut
    limits = [
        "only the root manifests, Makefile, justfile, .github/workflows and .gitlab-ci.yml are read; a nested "
        "package.json or another CI system is not",
        "a script, target or recipe is listed under build, test or check by its name (test, lint, format, "
        "typecheck, build and their prefixed forms); one with another name is not listed",
        "CI commands are the run:/script: commands as written (a block's continued or folded lines joined); "
        f"which job, matrix entry or condition runs them is not read; at most {MAX_CI} are listed, test "
        "commands first, and the rest are counted in omitted",
        "a layout count is git's file list (tracked and untracked, not ignored): its evidence is the folder, "
        "not a line",
        "built again on every call and never stored: the brief is as current as the files",
    ]
    return {"status": "ok" if rows else "empty", "budget": budget, "chars": len(text), "lines": kept,
            "total_lines": len(rows) + ci_cut,
            **({"omitted": omitted, "truncated": True} if omitted else {}),
            **({"ci_omitted": ci_cut} if ci_cut else {}),
            "text": text, "limits": limits}


def render(res: dict) -> str:
    if res["status"] == "empty":
        return ("brief: nothing to say - no pyproject.toml, package.json, Makefile, justfile, CI file, "
                "folder or convention file was found")
    return res["text"]
