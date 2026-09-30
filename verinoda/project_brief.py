"""The project brief (``verinoda brief``): a short summary of how the project is built, tested and laid out.

Agents keep a small "memory block" of project facts (the name, how to build and test it, where the code is, the
conventions). Written by hand it goes stale. :func:`brief` builds it again on every call from the files as they
are now, and every line names the ``file:line`` it was read from:

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
not fit is counted. Not the decision brief (``verinoda decide brief``). Read-only: nothing runs.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from verinoda.agentlint import TOOL_ROLES, Tree, _json, _key_rx, _line_of, _name_role, _read
from verinoda.agentlint import _quoted as _q

VERIFIED = "statically_verified"
DEFAULT_CHARS = 2000
MIN_CHARS, MAX_CHARS = 200, 20000
MAX_CMD = 100        # characters of one command or script body shown
MAX_CI = 10         # distinct CI commands
MAX_DIRS = 8         # top-level folders in the layout
SECTIONS = ("project", "build", "test", "check", "ci", "layout", "conventions")   # as printed
PRIORITY = ("project", "build", "test", "check", "layout", "conventions", "ci")   # what the budget keeps first
_SECTION_OF_ROLE = {"build": "build", "test": "test", "lint": "check", "format": "check", "typecheck": "check"}
_INSTRUCTIONS = ("AGENTS.md", "AGENT.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md",
                 ".cursorrules", ".windsurfrules", ".clinerules")
_PRE_COMMIT = ".pre-commit-config.yaml"
_RUN = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")
_SCRIPT = re.compile(r"^(\s*)(?:before_script|script|after_script):\s*(.*)$")
_TABLE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# shell plumbing a CI step runs around the commands (not how the project is built or tested)
_CI_SKIP = re.compile(r"^(echo|cd|set|export|if|fi|then|else|done|do|for|mkdir|printf|cat|ls|git config|\$\{\{)\b")
_INSTALL = re.compile(r"\b(?:pip|uv pip|uv add|npm|pnpm|yarn|bun)\s+(?:install|add|i|ci)\b")
_HOOK_ID =re.compile(r"^\s*-\s*id:\s*([\w.-]+)")


def _short(s: str, n: int = MAX_CMD) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 3] + "..."


def _line(section: str, text: str, at: str, status: str = VERIFIED) -> dict:
    return {"section": section, "text": text, "status": status, "evidence": [at]}


def _key_line(p: Path, table: str, key: str) -> int | None:
    """The line of ``key =`` inside TOML/INI table ``[table]`` (None when the table does not set it)."""
    inside = False
    rx = re.compile(r"^\s*[\"']?" + re.escape(key) + r"[\"']?\s*=")
    for i, ln in enumerate((_read(p) or "").splitlines(), 1):
        m = _TABLE.match(ln)
        if m:
            inside = m.group(1).strip() == table
        elif inside and rx.match(ln):
            return i
    return None


def _table_line(p: Path, table: str) -> int | None:
    for i, ln in enumerate((_read(p) or "").splitlines(), 1):
        m = _TABLE.match(ln)
        if m and m.group(1).strip() == table:
            return i
    return None


# -- the sections ------------------------------------------------------------------------------------------------

def _project(t: Tree) -> list[dict]:
    out = []
    pp = t.repo / "pyproject.toml"
    proj = t.pyproject().get("project") or {}
    if proj.get("name"):
        bits = [str(proj["name"])] + ([str(proj["version"])] if proj.get("version") else [])
        if proj.get("requires-python"):
            bits.append(f"Python {proj['requires-python']}")
        out.append(_line("project", "Python project " + ", ".join(bits),
                         f"pyproject.toml:{_key_line(pp, 'project', 'name') or 1}"))
    for tbl, what in (("scripts", "console script"), ("gui-scripts", "GUI script")):
        for name, target in (proj.get(tbl) or {}).items():
            ln = _key_line(pp, f"project.{tbl}", name) or _line_of(pp, _key_rx(name))
            out.append(_line("project", f"{what} `{name}` -> {target}", f"pyproject.toml:{ln}"))
    pj = t.repo / "package.json"
    data = _json(pj) if pj.is_file() else {}
    if data.get("name"):
        bits = [str(data["name"])] + ([str(data["version"])] if data.get("version") else [])
        node = (data.get("engines") or {}).get("node") if isinstance(data.get("engines"), dict) else None
        if node:
            bits.append(f"Node {node}")
        out.append(_line("project", "JavaScript package " + ", ".join(bits),
                         f"package.json:{_line_of(pj, _q('name'))}"))
    if isinstance(data.get("bin"), dict):
        for name, target in data["bin"].items():
            out.append(_line("project", f"bin `{name}` -> {target}", f"package.json:{_line_of(pj, _q(name))}"))
    elif isinstance(data.get("bin"), str):
        out.append(_line("project", f"bin -> {data['bin']}", f"package.json:{_line_of(pj, _q('bin'))}"))
    return out


def _commands(t: Tree) -> list[dict]:
    """Scripts, targets and recipes whose name gives a role, the build backend and the pytest settings."""
    out = []
    pp = t.repo / "pyproject.toml"
    backend = (t.pyproject().get("build-system") or {}).get("build-backend")
    if backend:
        out.append(_line("build", f"build backend {backend} (`python -m build`)",
                         f"pyproject.toml:{_key_line(pp, 'build-system', 'build-backend') or 1}"))
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
        p = t.repo / rel
        if not p.is_file():
            continue
        start = _table_line(p, table)
        if start is None:
            continue
        bits = []
        for key in ("testpaths", "addopts"):
            ln = _key_line(p, table, key)
            if ln:
                bits.append(_short((_read(p) or "").splitlines()[ln - 1].strip(), 60))
        return [_line("test", "pytest settings" + (": " + "; ".join(bits) if bits else ""), f"{rel}:{start}")]
    return []


def _ci_files(t: Tree) -> list[str]:
    return sorted(f for f in t.files if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", f)) + \
        [f for f in t.files if f == ".gitlab-ci.yml"]


def _ci(t: Tree) -> list[dict]:
    """The commands CI runs: ``run:`` values (one line, or each line of a ``|`` block) and ``script:`` items."""
    seen: dict[str, dict] = {}
    for rel in _ci_files(t):
        rx = _SCRIPT if rel.endswith(".gitlab-ci.yml") else _RUN
        lines = (_read(t.repo / rel) or "").splitlines()
        i = 0
        while i < len(lines):
            m = rx.match(lines[i])
            i += 1
            if not m:
                continue
            ind, val = len(m.group(1)), m.group(2).strip()
            cmds: list[tuple[int, str]] = []
            if val and not re.match(r"^[|>][+-]?\d*\s*(#.*)?$", val):
                cmds.append((i, val.strip("'\"")))
            else:   # a block (| or >) or a YAML list under script:
                while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > ind):
                    s = lines[i].strip()
                    i += 1
                    if s and not s.startswith("#"):
                        cmds.append((i, s[2:].strip().strip("'\"") if s.startswith("- ") else s))
            for ln, cmd in cmds:
                if cmd and not _CI_SKIP.match(cmd):
                    seen.setdefault(_short(cmd), _line("ci", f"`{_short(cmd)}`", f"{rel}:{ln}"))
    rows = list(seen.values())
    # the test commands first: a brief with little room keeps what CI checks a change with
    rows.sort(key=lambda r: 0 if _runs_test(r["text"].strip("`")) else 1)
    return rows[:MAX_CI]


def _runs_test(cmd: str) -> bool:
    """The command runs a test tool (``pytest``, ``python -m pytest``, ``npx jest``, ``npm test``, ``make
    test``), not one that installs it."""
    if _INSTALL.search(cmd):
        return False
    tools = "|".join(re.escape(k) for k, v in TOOL_ROLES.items() if v == "test")
    return bool(re.search(rf"(?:^|[\s/]|-m\s+)(?:{tools})(?:\s|$)", cmd) or
                re.search(r"\b(?:npm|pnpm|yarn|bun|make|just)\s+(?:run\s+)?test\b", cmd))


def _layout(t: Tree) -> list[dict]:
    per: dict[str, list[str]] = {}
    top = 0
    for f in t.files:
        parts = PurePosixPath(f).parts
        if len(parts) == 1:
            top += 1
        else:
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
        at = at or next((f"{f}:1" for f in (f"{d}/__init__.py", f"{d}/README.md", f"{d}/package.json") if f in fs),
                        f"{fs[0]}:1")
        out.append(_line("layout", f"{d}/: {role + ', ' if role else ''}{len(fs)} files ({main})", at))
    if per and len(per) > MAX_DIRS:
        rest = ranked[MAX_DIRS:]
        out.append(_line("layout", f"{len(rest)} more folders, {sum(len(v) for _, v in rest)} files",
                         f"{rest[0][1][0]}:1"))
    return out


def _folder_roles(t: Tree) -> dict[str, tuple[str, str]]:
    """Top-level folder -> (what a file says it is, where), in this order: the package a console script runs,
    pytest's ``testpaths``, a folder with an ``__init__.py``."""
    out: dict[str, tuple[str, str]] = {}
    pp = t.repo / "pyproject.toml"
    data = t.pyproject()
    for name, target in ((data.get("project") or {}).get("scripts") or {}).items():
        top = str(target).split(":")[0].split(".")[0]
        if top and f"{top}/__init__.py" in t.files:
            out.setdefault(top, (f"package of console script `{name}`",f"pyproject.toml:{_line_of(pp, _key_rx(name))}"))
    ini = ((data.get("tool") or {}).get("pytest") or {}).get("ini_options") or {}
    paths = ini.get("testpaths") or []
    for p in [paths] if isinstance(paths, str) else paths:
        top = PurePosixPath(str(p)).parts[0] if str(p).strip("./") else ""
        if top:
            ln = _key_line(pp, "tool.pytest.ini_options", "testpaths") or 1
            out.setdefault(top, ("pytest testpaths", f"pyproject.toml:{ln}"))
    for f in t.files:
        parts = PurePosixPath(f).parts
        if len(parts) == 2 and parts[1] == "__init__.py":
            out.setdefault(parts[0], ("Python package", f"{f}:1"))
    return out


def _conventions(t: Tree) -> list[dict]:
    out = []
    ec = t.repo / ".editorconfig"
    if ec.is_file():
        start = _table_line(ec, "*")
        if start:
            keys = [f"{k}={v}" for k in ("indent_style", "indent_size", "end_of_line", "charset")
                    for ln in [_key_line(ec, "*", k)] if ln
                    for v in [(_read(ec) or "").splitlines()[ln - 1].split("=", 1)[1].strip()]]
            if keys:
                out.append(_line("conventions", ".editorconfig [*]: " + ", ".join(keys), f".editorconfig:{start}"))
    ga = t.repo / ".gitattributes"
    if ga.is_file():
        for i, ln in enumerate((_read(ga) or "").splitlines(), 1):
            s = ln.strip()
            if s and not s.startswith("#") and re.search(r"(?:^|\s)(?:-?text\b|eol=|text=auto)", s):
                out.append(_line("conventions", f".gitattributes `{_short(s, 60)}`", f".gitattributes:{i}"))
                if sum(1 for r in out if r["text"].startswith(".gitattributes")) >= 2:
                    break
    pp = t.repo / "pyproject.toml"
    tool = t.pyproject().get("tool") or {}
    for name in ("ruff", "black", "isort", "flake8"):
        ll = (tool.get(name) or {}).get("line-length") if isinstance(tool.get(name), dict) else None
        if ll:
            out.append(_line("conventions", f"{name} line-length {ll}",
                             f"pyproject.toml:{_key_line(pp, f'tool.{name}', 'line-length') or 1}"))
    mypy = tool.get("mypy") if isinstance(tool.get("mypy"), dict) else {}
    if mypy.get("strict") is True:
        out.append(_line("conventions", "mypy strict", f"pyproject.toml:{_key_line(pp, 'tool.mypy', 'strict') or 1}"))
    ts = t.repo / "tsconfig.json"
    if ts.is_file():
        opts = _json(ts).get("compilerOptions")
        if isinstance(opts, dict) and "strict" in opts:
            out.append(_line("conventions", f"TypeScript strict: {str(opts['strict']).lower()}",
                             f"tsconfig.json:{_line_of(ts, _q('strict'))}"))
    pc = t.repo / _PRE_COMMIT
    if pc.is_file():
        ids = [m.group(1) for ln in (_read(pc) or "").splitlines() if (m := _HOOK_ID.match(ln))]
        if ids:
            out.append(_line("conventions", "pre-commit hooks: " + ", ".join(dict.fromkeys(ids[:8])),
                             f"{_PRE_COMMIT}:{_line_of(pc, _HOOK_ID)}"))
    for rel in _INSTRUCTIONS:
        if rel in t.files:
            out.append(_line("conventions", f"agent instructions in {rel} (checked by `verinoda agent-lint`)",
                             f"{rel}:1"))
    return out


# -- the budget --------------------------------------------------------------------------------------------------

def _row(r: dict) -> str:
    mark = "" if r["status"] == VERIFIED else f" [{r['status']}]"
    return f"- {r['text']} ({r['evidence'][0]}){mark}"


def render_text(rows: list[dict], *, budget: int | None = None, omitted: int = 0) -> str:
    out = ["# Project brief (from the files now; each line cites where it was read)"]
    for sec in SECTIONS:
        mine = [r for r in rows if r["section"] == sec]
        if mine:
            out.append(f"{sec}:")
            out += [_row(r) for r in mine]
    if omitted:
        out.append(f"({omitted} more line(s) over the {budget}-character budget: --max-chars)")
    return "\n".join(out)


def _fit(rows: list[dict], budget: int) -> tuple[list[dict], int]:
    """The rows that fit the budget: the first two of each section, then the rest, each in ``PRIORITY`` order."""
    order = sorted(range(len(rows)), key=lambda i: (
        0 if sum(1 for j in range(i) if rows[j]["section"] == rows[i]["section"]) < 2 else 1,
        PRIORITY.index(rows[i]["section"]), i))
    keep: set[int] = set()
    for i in order:
        trial = keep | {i}
        chosen = [rows[j] for j in sorted(trial)]
        left = len(rows) - len(trial)
        if len(render_text(chosen, budget=budget, omitted=left)) > budget:
            break   # a shorter line further down does not take the place of this one
        keep = trial
    return [rows[j] for j in sorted(keep)], len(rows) - len(keep)


def brief(repo: Path, *, max_chars: int = DEFAULT_CHARS, files: list[str] | None = None) -> dict:
    """``verinoda brief``: the project brief under ``max_chars`` characters, every line with evidence."""
    from verinoda.snapshot import list_files

    repo = Path(repo).resolve()
    budget = max(MIN_CHARS, min(MAX_CHARS, int(max_chars)))
    t = Tree(repo, list_files(repo) if files is None else files, Path.home())
    rows = _project(t) + _commands(t) + _ci(t) + _layout(t) + _conventions(t)
    rows.sort(key=lambda r: SECTIONS.index(r["section"]))   # stable: each section keeps its own order
    kept, omitted = _fit(rows, budget)
    text = render_text(kept, budget=budget, omitted=omitted)
    limits = [
        "only the root manifests, Makefile, justfile, .github/workflows and .gitlab-ci.yml are read; a nested "
        "package.json or another CI system is not",
        "a script, target or recipe is listed under build, test or check by its name (test, lint, format, "
        "typecheck, build and their prefixed forms); one with another name is not listed",
        "CI commands are the run:/script: lines as written; which job, matrix entry or condition runs them is not "
        "read",
        "the layout counts the files git lists (tracked and untracked, not ignored); the evidence of a folder is "
        "one file in it",
        "built again on every call and never stored: the brief is as current as the files",
    ]
    return {"status": "ok" if rows else "empty", "budget": budget, "chars": len(text), "lines": kept,
            "total_lines": len(rows), **({"omitted": omitted, "truncated": True} if omitted else {}),
            "text": text, "limits": limits}


def render(res: dict) -> str:
    if res["status"] == "empty":
        return ("brief: nothing to say - no pyproject.toml, package.json, Makefile, justfile, CI file, "
                "folder or convention file was found")
    return res["text"]
