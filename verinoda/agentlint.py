"""Agent instruction files checked against the repository.

AGENTS.md, CLAUDE.md, GEMINI.md, ``.github/copilot-instructions.md``, Cursor, Windsurf and Cline rules, and Claude
Code's memory files for the project tell an agent where things are and how to run them. They go stale the way
comments do: a file moves, a script is renamed, a tool leaves the dependencies, two files give two test commands.
:func:`lint` reads every line of them that names something and checks it against the tree, without a model:

- paths (code spans, links, ``@imports``, and prose paths with a folder in them) exist, with the case as written,
  and a ``file:LINE`` is within the file;
- scripts and targets: ``npm run X`` / ``pnpm X`` / ``yarn X`` in a package.json, ``make X`` in the Makefile,
  ``just X`` in the justfile, ``python -m X`` a module of the project, the standard library or a declared package,
  the first word the project's own console script;
- packages declared: ``pip install X`` / ``npm install X`` / ``uv add X``, the extras of ``pip install -e .[x]``
  and ``uv sync --extra x``, and the tools run by name (``pytest``, ``ruff``, ``npx eslint``) are in the manifests;
- the files agree: two files' test, lint, format, typecheck and build commands, and the package manager they use,
  against each other and against the lock file.

Every check is a record with a verdict (``ok``, ``wrong``, ``unknown``), a claim status and ``file:line``
evidence. What the tree or a manifest shows is ``statically_verified``; which package provides a command, and
whether two commands for one role disagree, are ``strong_inference`` at most (a make target may run the other
file's tool). A missing path that is git-ignored (built or kept locally), or on a line that tells the reader to
create it, is ``unknown``, never ``wrong``.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

VERIFIED = "statically_verified"
INFERRED = "strong_inference"
MAX_TEXT = 200
MAX_CHECKS = 1000   # records listed (wrong first); the summary counts them all
MAX_FUZZY = 50      # missing paths given close file names (a fuzzy match over every file name is slow)
# instruction files by name, at any depth of the project (a nested AGENTS.md governs its folder)
NAMED = {"AGENTS.md": "agents", "AGENT.md": "agents", "CLAUDE.md": "claude", "CLAUDE.local.md": "claude",
         "GEMINI.md": "gemini", ".cursorrules": "cursor", ".windsurfrules": "windsurf", ".clinerules": "cline"}
SHELL_LANGS = {"sh", "bash", "shell", "console", "zsh", "fish", "powershell", "ps1", "pwsh", "cmd", "bat",
               "shellsession", "terminal"}
PLAIN_LANGS = {"", "text", "txt", "plain"}   # read for known commands only (a tree diagram is not a command)
KNOWN_EXT = frozenset("""py pyi md mdc txt json jsonc json5 toml yaml yml ini cfg conf lock js mjs cjs jsx ts tsx mts
cts vue svelte css scss less html htm xml java kt kts gradle groovy scala go rs c h cc cpp hpp cs rb php swift lua sh
bash zsh ps1 psm1 bat cmd sql graphql proto env properties mcfunction mcmeta glsl fsh vsh ipynb csv tsv pdf png jpg
jpeg gif svg ico tf hcl r jl ex exs erl hs ml dart zig sol rst adoc patch diff mk cmake jar zip exe whl gz
tgz log""".split())
KNOWN_NAMES = frozenset({"Makefile", "GNUmakefile", "makefile", "Dockerfile", "justfile", "Justfile", "LICENSE",
                         "README", "gradlew", "mvnw", "Jenkinsfile", "Procfile", "Gemfile", "Pipfile", "Rakefile",
                         ".gitignore", ".gitattributes", ".editorconfig", ".env", ".npmrc", ".nvmrc"})
_DOMAIN = re.compile(r"^[\w-]+(\.[\w-]+)*\.(com|org|net|io|dev|ai|app|co|tr|gov|edu|me|sh|so)$", re.I)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)")
_SPAN = re.compile(r"(`+)(.+?)\1")
# the target in angle brackets may hold spaces (CommonMark): [x](<docs/My File.md>)
_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^)\s]+))(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_IMPORT = re.compile(r"(?<![\w`/@.\\])@((?:~/|\.{1,2}/|/)?[\w.-]+(?:/[\w.-]+)*)")
# the whole token or nothing: ``docs/drafts/<id>.md`` is a placeholder, not the folder docs/drafts/
_PROSE_PATH = re.compile(r"(?<![\w@/.:\\-])((?:~/|\.{1,2}/)?[\w.-]+(?:/[\w.-]+)+/?(?::\d+(?:-\d+)?)?)"
                         r"(?![\w/<{$*\\-])")
_WIKI = re.compile(r"\[\[([^\]|#\n]+)(?:[|#][^\]\n]*)?\]\]")   # a memory file's link to another: [[name]]
_PROMPT = re.compile(r"^\s*(?:\$|>|%|PS[^>]{0,40}>)\s+")
_LINE_SUFFIX = re.compile(r"(?::(\d+)(?:-(\d+))?|#L(\d+)(?:-L?(\d+))?)$")
_CREATE = re.compile(r"\b(create[sd]?|creating|write|writes|writing|generate[sd]?|generating|output (?:to|in|into)|"
                     r"new file|will be (?:created|written|generated))\b", re.I)
_LOCKS = {"package-lock.json": "npm", "npm-shrinkwrap.json": "npm", "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn",
          "bun.lockb": "bun", "bun.lock": "bun", "uv.lock": "uv", "poetry.lock": "poetry", "Pipfile.lock": "pipenv",
          "pdm.lock": "pdm"}
JS_MANAGERS = ("npm", "pnpm", "yarn", "bun")
PY_MANAGERS = ("uv", "poetry", "pipenv", "pdm")
# a package manager's own commands; any other word after yarn / pnpm / bun is a package.json script
_PM_BUILTINS = frozenset("""install i add remove rm uninstall un update up upgrade outdated dlx exec x create init link
unlink publish pack audit why list ls info view config set global store cache import prune rebuild patch env
workspace workspaces recursive version versions login logout help bin root licenses dedupe dedup fetch deploy setup
self-update node plugin constraints explain search tag team ci run run-script test t tst start stop restart""".split())
_PM_VALUE_FLAGS = ("--prefix", "-C", "--dir", "-w", "--workspace", "--filter", "-F", "--cwd")
_PM_ALL_FLAGS = ("-r", "--recursive", "-ws", "--workspaces")   # every package of the workspace
# bun's own commands where yarn and pnpm would run a script: `bun test` is bun's test runner
_BUN_BUILTINS = frozenset("test build pm repl x upgrade init create link outdated publish patch".split())
# `uv run` / `poetry run` options that take a value (the value is not the command)
_RUN_VALUE_FLAGS = ("--extra", "--group", "--only-group", "--no-group", "--no-extra", "--with", "--with-editable",
                    "--with-requirements", "--package", "--python", "-p", "--directory", "--project", "--env-file",
                    "--index", "--from", "--config-file", "--cache-dir")
# tools an instruction file runs by name, and the package that provides each (any of them declared will do)
PY_TOOLS = {"pytest": ("pytest",), "ruff": ("ruff",), "mypy": ("mypy",), "black": ("black",), "flake8": ("flake8",),
            "isort": ("isort",), "pylint": ("pylint",), "coverage": ("coverage", "pytest-cov"),
            "bandit": ("bandit",), "pyright": ("pyright",), "sphinx-build": ("sphinx",), "mkdocs": ("mkdocs",)}
JS_TOOLS = {"eslint": ("eslint",), "prettier": ("prettier",), "tsc": ("typescript",), "jest": ("jest",),
            "vitest": ("vitest",), "mocha": ("mocha",), "playwright": ("@playwright/test", "playwright"),
            "vite": ("vite",), "next": ("next",), "webpack": ("webpack", "webpack-cli"), "biome": ("@biomejs/biome",),
            "cypress": ("cypress",), "ts-node": ("ts-node",), "tsx": ("tsx",), "stylelint": ("stylelint",),
            "rollup": ("rollup",), "esbuild": ("esbuild",), "turbo": ("turbo",), "nx": ("nx",)}
TOOL_ROLES = {"pytest": "test", "unittest": "test", "jest": "test", "vitest": "test", "mocha": "test",
              "ruff": "lint", "flake8": "lint", "pylint": "lint", "eslint": "lint", "black": "format",
              "isort": "format", "prettier": "format", "ruff format": "format", "mypy": "typecheck",
              "pyright": "typecheck", "tsc": "typecheck"}
_ROLE_NAMES = (("test", r"tests?(?:[:_-].*)?"), ("lint", r"lint(?:[:_-].*)?"),
               ("format", r"(?:format|fmt)(?:[:_-].*)?"),
               ("typecheck", r"(?:type-?check|types|tsc|check-types)(?:[:_-].*)?"), ("build", r"build(?:[:_-].*)?"))
# import name -> distribution name, where they differ
_DIST_OF = {"yaml": "pyyaml", "PIL": "pillow", "sklearn": "scikit-learn", "cv2": "opencv-python",
            "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "dotenv": "python-dotenv", "docx": "python-docx",
            "jwt": "pyjwt", "serial": "pyserial", "attr": "attrs", "git": "gitpython"}


@dataclass
class Doc:
    rel: str            # as evidence names it: repository-relative, or ~/... for a file outside the repository
    path: Path
    kind: str           # agents | claude | gemini | copilot | cursor | windsurf | cline | memory | file
    bases: list[Path]   # folders a relative path is read from, in order
    link_base: Path     # the folder a Markdown link is relative to (its own)
    lines: list[str] = field(default_factory=list)
    scope: str = ""     # the folder it governs, repository-relative ("" the whole project)


@dataclass
class Check:
    kind: str           # path | link | import | script | target | module | package | extra | tool | command | task
    name: str           # | agreement
    doc: Doc
    line: int
    verdict: str        # ok | wrong | unknown
    status: str
    why: str
    evidence: list[str] = field(default_factory=list)   # further file:line (the manifest, the other file)
    nearest: list[str] = field(default_factory=list)

    def record(self) -> dict:
        at = f"{self.doc.rel}:{self.line}"
        text = self.doc.lines[self.line - 1].strip() if 0 < self.line <= len(self.doc.lines) else ""
        return {"verdict": self.verdict, "status": self.status, "kind": self.kind, "name": self.name, "at": at,
                "text": text[:MAX_TEXT] + ("..." if len(text) > MAX_TEXT else ""), "why": self.why,
                "evidence": [at, *self.evidence], **({"nearest": self.nearest} if self.nearest else {})}


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _doc_lines(text: str) -> list[str]:
    """The lines of an instruction file, those of Verinoda's own marked blocks blank (the installer's text,
    not the project's; numbering kept)."""
    from verinoda.selffiles import find_block

    out: list[str] = []
    while (m := find_block(text)) is not None:
        out += text[:m.start()].splitlines() + [""] * len(m.group(0).splitlines())
        text = text[m.end():]
    return out + text.splitlines()


def _line_of(p: Path, needle: str | re.Pattern, start: int = 1) -> int:
    for i, ln in enumerate((_read(p) or "").splitlines(), 1):
        if i >= start and (needle.search(ln) if isinstance(needle, re.Pattern) else needle in ln):
            return i
    return 1


def _quoted(name: str) -> str:
    return '"' + name + '"'


def _key_rx(name: str) -> re.Pattern:
    """A TOML key's line: ``name =`` or ``"name" =``."""
    return re.compile(r"^\s*[\"']?" + re.escape(name) + r"[\"']?\s*=")


def _dep_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()


def _dep_line(p: Path, name: str) -> int | None:
    """The line that declares ``name`` in a manifest: a requirement string (``"pytest>=8"``, a requirements
    line) or a TOML key (``pytest = "^8"``), not a comment or a ``[tool.pytest]`` table that mentions it."""
    nm = r"[-_.]+".join(re.escape(x) for x in re.split(r"[-_.]+", name) if x)
    rx = re.compile(r"(?:^\s*|[\"'])" + nm + r"\s*(?:\[|[<>=!~;@\"',]|$)", re.I)
    for i, ln in enumerate((_read(p) or "").splitlines(), 1):
        if not ln.lstrip().startswith("#") and rx.search(ln.split(" #")[0]):
            return i
    return None


def _glob_rx(pat: str) -> re.Pattern:
    """A glob as git and most tools read it: ``*`` and ``?`` stay within a folder, ``**/`` is any number of
    folders (none too), a trailing ``**`` is everything below."""
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif pat.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pat[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pat[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pat[i]), i + 1
    return re.compile(out + r"\Z")


def _toml(p: Path) -> dict:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - py3.10
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        return tomllib.loads(_read(p) or "")
    except Exception:  # noqa: BLE001 - an unreadable manifest declares nothing
        return {}


def _json(p: Path) -> dict:
    try:
        data = json.loads(_read(p) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# -- the files ---------------------------------------------------------------------------------------------------

def _kind(rel: str) -> str | None:
    p = PurePosixPath(rel)
    slashed = "/" + rel
    if p.name in NAMED:
        return NAMED[p.name]
    if rel == ".github/copilot-instructions.md" or (rel.startswith(".github/instructions/")
                                                     and p.name.endswith(".instructions.md")):
        return "copilot"
    if "/.cursor/rules/" in slashed and p.suffix in (".mdc", ".md"):
        return "cursor"
    if "/.windsurf/rules/" in slashed and p.suffix == ".md":
        return "windsurf"
    if "/.clinerules/" in slashed and p.suffix == ".md":
        return "cline"
    return None


def _scope(rel: str) -> str:
    """The folder an instruction file governs: a named file's own folder, a rules folder's parent
    (``web/.cursor/rules/x.mdc`` governs ``web``); ``.github`` files govern the whole project."""
    parts = PurePosixPath(rel).parts[:-1]
    for marker in (".cursor", ".windsurf", ".clinerules", ".github"):
        if marker in parts:
            parts = parts[:parts.index(marker)]
            break
    return "/".join(parts)


def _main_worktree(repo: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return Path(out).parent if r.returncode == 0 and out and Path(out).name == ".git" else None


def memory_dirs(repo: Path, home: Path | None = None) -> list[Path]:
    """Claude Code's memory folders for the project: ``~/.claude/projects/<path with every other character a
    dash>/memory``, for the repository and, in a worktree, for its main checkout."""
    home = Path.home() if home is None else Path(home)
    roots = [Path(repo)]
    main = _main_worktree(Path(repo))
    if main is not None and main.resolve() != Path(repo).resolve():
        roots.append(main)
    out = []
    for r in roots:
        d = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(r)) / "memory"
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def _shown(p: Path, repo: Path, home: Path) -> str:
    try:
        return p.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        pass
    try:
        under = p.resolve().relative_to(home.resolve()).as_posix()
        return "~" if under == "." else "~/" + under
    except ValueError:
        return p.as_posix()


def instruction_files(repo: Path, files: list[str], *, extra: list[str] | None = None, memory: bool = True,
                      home: Path | None = None) -> list[Doc]:
    """The instruction files of the project (by name, at any depth; not under test folders), then Claude Code's
    memory files for it, then ``extra`` (paths given by the user)."""
    from verinoda.testcode import is_test_file

    repo = Path(repo)
    home = Path.home() if home is None else Path(home)
    docs: list[Doc] = []
    rels = [f for f in files if _kind(f) and not is_test_file(f)]
    if (repo / "CLAUDE.local.md").is_file() and "CLAUDE.local.md" not in rels:
        rels.append("CLAUDE.local.md")   # personal and usually git-ignored, but read by the agent
    for rel in sorted(rels):
        p = repo / rel
        d = p.parent
        docs.append(Doc(rel, p, _kind(rel) or "file", list(dict.fromkeys([d, repo])), d, scope=_scope(rel)))
    for md in memory_dirs(repo, home) if memory else []:
        for p in sorted(md.glob("*.md")):
            docs.append(Doc(_shown(p, repo, home), p, "memory", [repo, md], md))
    for e in extra or []:
        p = Path(e).expanduser()
        p = p if p.is_absolute() else repo / p
        if not p.is_file():
            raise FileNotFoundError(f"{e}: no such file")
        if any(x.path.resolve() == p.resolve() for x in docs):
            continue
        docs.append(Doc(_shown(p, repo, home), p, "file", list(dict.fromkeys([p.parent, repo])), p.parent))
    for doc in docs:
        doc.lines = _doc_lines(_read(doc.path) or "")
    return docs


# -- what the repository has ---------------------------------------------------------------------------------------

class Tree:
    """The project as the checks read it: files, manifests, scripts, targets, locks; read once, lazily."""

    def __init__(self, repo: Path, files: list[str], home: Path):
        self.repo = Path(repo)
        self.home = home
        self.files = files
        self._dirs: dict[Path, dict[str, str] | None] = {}
        self._memo: dict[str, object] = {}
        self._real: dict[Path, str] = {}
        self._root = os.path.normcase(str(self.repo.resolve()))

    def memo(self, key: str, make):
        if key not in self._memo:
            self._memo[key] = make()
        return self._memo[key]

    def rel_of(self, base: Path, v: str = "") -> str | None:
        """``base / v`` relative to the repository (posix; "" the root), or None outside it. A folder is
        resolved once (resolving is slow on a synced drive), the rest joined as text."""
        if base not in self._real:
            self._real[base] = str(base.resolve())
        full = os.path.normpath(os.path.join(self._real[base], v)) if v else self._real[base]
        root, low = self._root, os.path.normcase(full)
        if low == root:
            return ""
        if not low.startswith(root.rstrip(os.sep) + os.sep):
            return None
        return full[len(root.rstrip(os.sep)) + 1:].replace(os.sep, "/")

    # paths
    def entries(self, d: Path) -> dict[str, str] | None:
        """Lower-cased name -> the name on disk, for folder ``d``."""
        if d not in self._dirs:
            try:
                names = os.listdir(d)
            except OSError:
                self._dirs[d] = None
            else:
                self._dirs[d] = {n.lower(): n for n in names} | {n: n for n in names}
        return self._dirs[d]

    def locate(self, base: Path, rel: str) -> tuple[Path | None, str | None]:
        """(the path, None) when ``rel`` exists under ``base`` as written; (None, the spelling on disk) when it
        exists only in another case (a case-insensitive disk finds it, a Linux checkout does not)."""
        p, other = base, False
        for part in PurePosixPath(rel).parts:
            if part in (".", ""):
                continue
            if part == "..":
                p = p.parent
                continue
            ents = self.entries(p)
            if ents is None:
                return None, None
            if part in ents and ents[part] == part:
                p = p / part
            elif part.lower() in ents:
                other = True
                p = p / ents[part.lower()]
            else:
                return None, None
        if other:
            try:
                return None, p.relative_to(base).as_posix()
            except ValueError:
                return None, p.as_posix()
        return p, None

    def basenames(self) -> dict[str, list[str]]:
        def make():
            out: dict[str, list[str]] = {}
            for f in self.files:
                out.setdefault(PurePosixPath(f).name, []).append(f)
            return out
        return self.memo("basenames", make)

    def dirs(self) -> set[str]:
        def make():
            out: set[str] = set()
            for f in self.files:
                parts = PurePosixPath(f).parts[:-1]
                for i in range(1, len(parts) + 1):
                    out.add("/".join(parts[:i]))
            return out
        return self.memo("dirs", make)

    def dir_names(self) -> dict[str, list[str]]:
        def make():
            out: dict[str, list[str]] = {}
            for d in sorted(self.dirs()):
                out.setdefault(PurePosixPath(d).name, []).append(d)
            return out
        return self.memo("dir_names", make)

    def ignored(self, rels: list[str]) -> set[str]:
        """The repository-relative paths git ignores (made by a build, or kept locally)."""
        if not rels:
            return set()
        try:   # NUL-separated bytes: text mode on Windows would send "\r\n", and git would read the "\r" as a name
            r = subprocess.run(["git", "check-ignore", "--no-index", "-z", "--stdin"], cwd=self.repo,
                               input="\0".join(rels).encode("utf-8") + b"\0", capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return set()
        return {s for s in r.stdout.decode("utf-8", errors="replace").split("\0") if s}

    # manifests
    def pyproject(self) -> dict:
        return self.memo("pyproject", lambda: _toml(self.repo / "pyproject.toml")
                         if (self.repo / "pyproject.toml").is_file() else {})

    def package_jsons(self) -> list[str]:
        return self.memo("package_jsons", lambda: sorted(
            f for f in self.files if PurePosixPath(f).name == "package.json" and "node_modules" not in f.split("/")))

    def has_python_manifest(self) -> bool:
        return self.memo("has_python_manifest", lambda: any(
            (self.repo / n).is_file() for n in ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile")) or
            any(self.repo.glob("requirements*.txt")))

    def python_deps(self) -> dict[str, str]:
        """Declared Python distributions (normalised) -> where: the project's dependencies, extras, dependency
        groups, uv/pdm/poetry dev dependencies and requirements files."""
        def make():
            from verinoda import research

            out: dict[str, str] = {}
            try:
                items = research.dependencies(self.repo).get("items") or []
            except Exception:  # noqa: BLE001 - an unreadable manifest declares nothing
                items = []
            for it in items:
                if it.get("ecosystem") == "python":
                    ln = _dep_line(self.repo / it["path"], it["name"]) if it.get("path") else None
                    out.setdefault(_dep_key(it["name"]), f"{it['path']}:{ln}" if ln else it["at"])
            data = self.pyproject()
            pp = self.repo / "pyproject.toml"
            reqs: list[str] = []
            for grp in (data.get("dependency-groups") or {}).values():
                reqs += [s for s in grp or [] if isinstance(s, str)]
            tool = data.get("tool") or {}
            reqs += [s for s in ((tool.get("uv") or {}).get("dev-dependencies") or []) if isinstance(s, str)]
            for grp in ((tool.get("pdm") or {}).get("dev-dependencies") or {}).values():
                reqs += [s for s in grp or [] if isinstance(s, str)]
            poetry = tool.get("poetry") or {}
            names = list((poetry.get("dev-dependencies") or {}).keys())
            for grp in (poetry.get("group") or {}).values():
                names += list(((grp or {}).get("dependencies") or {}).keys())
            names += [m.group(1) for s in reqs if (m := re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", s))]
            for n in names:
                if n.lower() != "python":
                    out.setdefault(_dep_key(n), f"pyproject.toml:{_dep_line(pp, n) or 1}")
            return out
        return self.memo("python_deps", make)

    def npm_deps(self) -> dict[str, str]:
        def make():
            out: dict[str, str] = {}
            for rel in self.package_jsons():
                data = _json(self.repo / rel)
                for sect in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                    for name in (data.get(sect) or {}) if isinstance(data.get(sect), dict) else ():
                        out.setdefault(name.lower(), f"{rel}:{_line_of(self.repo / rel, _quoted(name))}")
            return out
        return self.memo("npm_deps", make)

    def own_names(self) -> set[str]:
        """The project's own distribution and package names (installing the project is not an undeclared
        package)."""
        def make():
            out = {_dep_key((self.pyproject().get("project") or {}).get("name") or "")}
            for rel in self.package_jsons():
                out.add(str(_json(self.repo / rel).get("name") or "").lower())
            return out - {""}
        return self.memo("own_names", make)

    def own_scripts(self) -> dict[str, str]:
        """Console scripts the project declares (``[project.scripts]``, poetry scripts, package.json ``bin``)."""
        def make():
            out: dict[str, str] = {}
            data = self.pyproject()
            pp = self.repo / "pyproject.toml"
            for tbl in ((data.get("project") or {}).get("scripts"), (data.get("project") or {}).get("gui-scripts"),
                        ((data.get("tool") or {}).get("poetry") or {}).get("scripts")):
                for name in tbl or {}:
                    out.setdefault(name, f"pyproject.toml:{_line_of(pp, _key_rx(name))}")
            for rel in self.package_jsons():
                data = _json(self.repo / rel)
                b = data.get("bin")
                names = [str(data.get("name") or "").split("/")[-1]] if isinstance(b, str) else \
                    list(b) if isinstance(b, dict) else []
                for name in names:
                    if name:
                        out.setdefault(name, f"{rel}:{_line_of(self.repo / rel, _quoted('bin'))}")
            return out
        return self.memo("own_scripts", make)

    def scripts(self, rel: str) -> dict[str, tuple[str, int]]:
        """package.json ``scripts``: name -> (body, line)."""
        def make():
            p = self.repo / rel
            sc = _json(p).get("scripts")
            if not isinstance(sc, dict):
                return {}
            start = _line_of(p, '"scripts"')
            return {k: (str(v), _line_of(p, f'"{k}"', start)) for k, v in sc.items()}
        return self.memo(f"scripts:{rel}", make)

    def make_targets(self, rel: str) -> tuple[dict[str, tuple[int, str]], bool]:
        """Makefile targets -> (line, recipe text); and whether it includes other files or has pattern rules
        (a target not found there may still exist)."""
        def make():
            out: dict[str, tuple[int, str]] = {}
            open_ended = False
            cur: list[str] = []
            for i, ln in enumerate((_read(self.repo / rel) or "").splitlines(), 1):
                if re.match(r"^-?s?include\s", ln):
                    open_ended = True
                if ln.startswith("\t"):
                    for c in cur:
                        out[c] = (out[c][0], out[c][1] + " " + ln.strip())
                    continue
                m = re.match(r"^([^\s:#=][^:#=]*?)\s*::?(?!=)", ln)
                if m:
                    cur = []
                    for t in m.group(1).split():
                        if "%" in t:
                            open_ended = True
                        elif not t.startswith("."):
                            out.setdefault(t, (i, ""))
                            cur.append(t)
                elif ln.strip():
                    cur = []
            return out, open_ended
        return self.memo(f"make:{rel}", make)

    def just_recipes(self, rel: str) -> tuple[dict[str, int], bool]:
        def make():
            out: dict[str, int] = {}
            open_ended = False
            for i, ln in enumerate((_read(self.repo / rel) or "").splitlines(), 1):
                if re.match(r"^(import|mod)\b", ln):
                    open_ended = True
                a = re.match(r"^alias\s+([A-Za-z_][\w-]*)\s*:=", ln)
                if a:   # `alias t := test`: just runs the recipe by either name
                    out.setdefault(a.group(1), i)
                    continue
                m = re.match(r"^@?([A-Za-z_][\w-]*)(?:\s+[^:=]*)?:(?!=)", ln)
                if m and not ln.startswith((" ", "\t")):
                    out.setdefault(m.group(1), i)
            return out, open_ended
        return self.memo(f"just:{rel}", make)

    def locks(self) -> dict[str, str]:
        """Package manager -> the lock file (or package.json ``packageManager``) that pins it."""
        def make():
            out: dict[str, str] = {}
            for name, mgr in _LOCKS.items():
                if (self.repo / name).is_file():
                    out.setdefault(mgr, f"{name}:1")
            pm = str(_json(self.repo / "package.json").get("packageManager") or "")
            m = re.match(r"(npm|pnpm|yarn|bun)@", pm)
            if m:
                out.setdefault(m.group(1), f"package.json:{_line_of(self.repo / 'package.json', 'packageManager')}")
            return out
        return self.memo("locks", make)


# -- reading the lines -------------------------------------------------------------------------------------------

@dataclass
class Mention:
    line: int
    how: str        # code | prose | link | import | command | fence
    value: str
    shell: bool = True   # a command from a shell block or a code span (its arguments are read for paths)
    block: int = 0       # the line a fence opens on (its lines share a working folder: `cd` carries)


def mentions(doc: Doc) -> list[Mention]:
    """What each line names: links, ``@imports`` (CLAUDE.md), code spans, commands in shell blocks, prose paths."""
    out: list[Mention] = []
    fence: tuple[str, str] | None = None
    block = 0
    front = bool(doc.lines) and doc.lines[0].strip() == "---"
    for i, raw in enumerate(doc.lines, 1):
        if front:   # YAML front matter (memory files, Cursor rules): metadata, not instructions
            if i > 1 and raw.strip() == "---":
                front = False
            continue
        m = _FENCE.match(raw)
        if m and fence is None:
            fence = (m.group(1)[0] * 3, m.group(2).lower())
            block = i
            continue
        if fence is not None:
            if raw.strip().startswith(fence[0]):
                fence = None
                continue
            lang = fence[1]
            if lang in SHELL_LANGS or lang in PLAIN_LANGS:
                s = _PROMPT.sub("", raw).strip()
                if s and not s.startswith(("#", "//", "REM ", "::")):
                    out.append(Mention(i, "fence", s, shell=lang in SHELL_LANGS, block=block))
            continue
        line = raw
        if line.lstrip().startswith("<!--"):
            continue
        for lm in _LINK.finditer(line):
            out.append(Mention(i, "link", (lm.group(1) or lm.group(2)).strip()))
        line = _LINK.sub(" ", line)
        if doc.kind == "memory":
            for wm in _WIKI.finditer(line):
                out.append(Mention(i, "link", wm.group(1).strip() + ".md"))
            line = _WIKI.sub(" ", line)
        for sm in _SPAN.finditer(line):
            v = sm.group(2).strip()
            out.append(Mention(i, "command" if " " in v else "code", v))
        line = _SPAN.sub(" ", line)
        if doc.kind == "claude":   # Claude Code reads ``@path`` in CLAUDE.md as an import
            for im in _IMPORT.finditer(line):
                if "/" in im.group(1) or "." in im.group(1):
                    out.append(Mention(i, "import", im.group(1)))
            line = _IMPORT.sub(" ", line)
        for pm in _PROSE_PATH.finditer(line):
            out.append(Mention(i, "prose", pm.group(1).rstrip(".")))
    return out


# -- the checks --------------------------------------------------------------------------------------------------

class Linter:
    def __init__(self, tree: Tree):
        self.t = tree
        self.checks: list[Check] = []
        self.roles: list[tuple[str, str, Doc, int]] = []      # (role, key, doc, line)
        self.managers: list[tuple[str, Doc, int]] = []        # (package manager, doc, line)
        self._seen: set[tuple] = set()
        self._expand: dict[str, str] = {}   # a script or target -> what it runs (its body, its recipe)
        self._missing: list[tuple] = []     # paths not found, settled together (one git call)

    def add(self, c: Check) -> None:
        key = (c.doc.rel, c.line, c.kind, c.name)
        if key not in self._seen:
            self._seen.add(key)
            self.checks.append(c)

    # paths
    def path_like(self, v: str, how: str) -> bool:
        """Does ``v`` name a path? A folder in it and a known extension or an existing first folder; a bare
        name only in code with a known extension or file name. URLs, options, placeholders, domains, slash
        commands and ``ns:id`` names are not paths."""
        if not v or any(c in v for c in "<>{}$%|\"'`=,;()[] \t") or "..." in v or "://" in v or v.startswith("-"):
            return False
        v = v.replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", v):
            return how != "prose" or "/" in v[3:]
        if ":" in _LINE_SUFFIX.sub("", v):
            return False
        v = _LINE_SUFFIX.sub("", v).split("#")[0].split("::")[0].rstrip("/")
        if not v or v in (".", ".."):
            return False
        parts = [p for p in v.split("/") if p not in ("", ".")]
        if not parts or re.fullmatch(r"[\d./]+", v):
            return False
        if len(parts) > 1 and not v.startswith(("./", "../", "~/")) and _DOMAIN.match(parts[0]):
            return False
        if v.startswith("/"):
            return len(parts) >= 2 and how != "prose"
        name = parts[-1]
        ext = name.rsplit(".", 1)[1].lower() if "." in name.strip(".") else ""
        known = ext in KNOWN_EXT or name in KNOWN_NAMES
        if len(parts) == 1 and not v.startswith(("./", "../", "~/")):
            return how in ("code", "link", "import", "arg") and known
        if v.startswith(("./", "../", "~/")) or known:
            return True
        return parts[0] in self.t.dirs() or (self.t.repo / parts[0]).exists()

    def names_existing_path(self, doc: Doc, v: str) -> bool:
        """Is a code span with a space in it a path that exists (``docs/My File.md``), not a command?"""
        v = _LINE_SUFFIX.sub("", v.replace("\\", "/"))
        if "/" not in v or re.search(r"[|&;<>$`]|\s-", v):
            return False
        return any(self.t.locate(b, v.rstrip("/"))[0] is not None for b in doc.bases)

    def check_path(self, doc: Doc, m: Mention, v: str, kind: str = "path", bases: list[Path] | None = None) -> None:
        raw = v
        v = v.replace("\\", "/")
        lm = _LINE_SUFFIX.search(v)
        first = last = None
        if lm and not re.match(r"^[A-Za-z]:$", v[:lm.start()] or "x"):
            first = int(lm.group(1) or lm.group(3))
            last = int(lm.group(2) or lm.group(4) or first)
            v = v[:lm.start()]
        v = v.split("#")[0].split("::")[0]
        if not v:
            return
        bases = doc.bases if bases is None else bases
        outside = None
        if v.startswith("~/"):
            if doc.kind == "memory":   # this user's own notes: their home is the one that counts
                bases, v = [self.t.home], v[2:]
            else:
                outside = "a path in the home folder of whoever reads the file, not in the repository"
        elif v.startswith("/") and not v.startswith("//"):
            top = v[1:].split("/")[0]
            if kind == "link" or (self.t.entries(self.t.repo) or {}).get(top) == top:
                bases, v = [self.t.repo], v[1:]   # root-relative, as GitHub reads a link: from the repository root
            else:
                outside = "an absolute path outside the repository"
        elif re.match(r"^[A-Za-z]:/", v):
            rel = self.t.rel_of(Path(v[:3]), v[3:]) if os.name == "nt" else None
            if rel is not None:
                bases, v = [self.t.repo], rel
            elif os.name == "nt" and doc.kind == "memory":
                bases, v = [Path(v[:3])], v[3:]
            else:
                outside = "an absolute path outside the repository"
        elif not bases:
            outside = "relative to a folder an earlier `cd` moved to, which is not followed"
        if outside:
            self.add(Check(kind, raw, doc, m.line, "unknown", "unknown", f"not checked: {outside}"))
            return
        if "*" in v or "?" in v:
            self._check_glob(doc, m, raw, v, kind, bases)
            return
        found, case = None, None
        for b in bases:
            found, c = self.t.locate(b, v.rstrip("/"))
            case = case or c
            if found is not None:
                break
        if found is None and "/" not in v.rstrip("/") and m.how != "link":
            hits = self.t.basenames().get(v)
            if hits:   # a bare file name: somewhere in the project will do
                self.add(Check(kind, raw, doc, m.line, "ok", VERIFIED, f"exists: {', '.join(hits[:3])}",
                               [f"{h}:1" for h in hits[:3]]))
                return
        if found is not None:
            if first is not None and found.is_file():
                n = len((_read(found) or "").splitlines())
                if last > n or first < 1:
                    self.add(Check(kind, raw, doc, m.line, "wrong", VERIFIED,
                                   f"{_shown(found, self.t.repo, self.t.home)} has {n} lines, fewer than {last}"))
                    return
            self.add(Check(kind, raw, doc, m.line, "ok", VERIFIED, "exists",
                           [f"{_shown(found, self.t.repo, self.t.home)}:{first or 1}"] if found.is_file() else []))
            return
        if case:
            self.add(Check(kind, raw, doc, m.line, "wrong", VERIFIED,
                           f"exists only as {case} (the case differs: found on a case-insensitive disk, not on "
                           f"Linux or in git)"))
            return
        self._missing.append((doc, m, raw, v, kind, bases))

    def _check_glob(self, doc, m, raw, v, kind, bases) -> None:
        for b in bases:
            pre = self.t.rel_of(b)
            if pre is None:
                continue
            rx = _glob_rx((f"{pre}/{v}" if pre else v).rstrip("/"))
            hit = next((f for f in self.t.files if rx.match(f)), None) or                 next((d for d in sorted(self.t.dirs()) if rx.match(d)), None)
            if hit:
                self.add(Check(kind, raw, doc, m.line, "ok", VERIFIED, f"matches {hit}", [f"{hit}:1"]))
                return
        self._missing.append((doc, m, raw, v, kind, bases))

    def settle_missing(self) -> None:
        """The paths not found: git-ignored ones and those on a line about creating them are unknown, the rest
        wrong, with the nearest file names."""
        rel_of: list[str | None] = []
        for _doc, _m, _raw, v, _k, bases in self._missing:
            rel_of.append(next((r for b in bases if (r := self.t.rel_of(b, v)) is not None), None))
        ignored = self.t.ignored(sorted({r for r in rel_of if r}))
        near_memo: dict[tuple[str, str], list[str]] = {}
        shown: dict[Path, str] = {}
        for (doc, m, raw, v, kind, bases), rel in zip(self._missing, rel_of):
            text = doc.lines[m.line - 1] if 0 < m.line <= len(doc.lines) else ""
            if rel is not None and (rel in ignored or any(rel.startswith(i.rstrip("/") + "/") for i in ignored)):
                self.add(Check(kind, raw, doc, m.line, "unknown", "unknown",
                               "not found; git ignores it (made by a build or kept locally)"))
                continue
            if _CREATE.search(text):
                self.add(Check(kind, raw, doc, m.line, "unknown", "unknown",
                               "not found; the line may tell the reader to create it"))
                continue
            name = PurePosixPath(v.rstrip("/")).name
            tail = "/" + v.strip("/").lstrip("./")
            if (name, tail) not in near_memo:
                same = [*self.t.basenames().get(name, []), *self.t.dir_names().get(name, [])]
                moved = [f for f in same if ("/" + f).endswith(tail)][:3]
                near = [] if moved or len(near_memo) >= MAX_FUZZY else \
                    difflib.get_close_matches(name, list(self.t.basenames()), n=3, cutoff=0.75)
                near_memo[(name, tail)] = moved or [h for n in near for h in self.t.basenames()[n][:2]][:3]
            if bases and bases[0] != self.t.repo and bases[0] not in shown:
                shown[bases[0]] = _shown(bases[0], self.t.repo, self.t.home)
            self.add(Check(kind, raw, doc, m.line, "wrong", VERIFIED,
                           "no such file or folder" + (f" (from {shown[bases[0]]})"
                                                       if bases and bases[0] != self.t.repo else ""),
                           nearest=near_memo[(name, tail)]))

    # commands
    def command(self, doc: Doc, m: Mention, cwd: list[Path] | None = None) -> list[Path]:
        """One command line: split on ``&&``, ``||``, ``;`` and ``|``; ``cd`` moves where the rest is read.
        Returns the folder it ends in (the next line of the same shell block starts there); an empty list is a
        folder that cannot be followed (``cd -``, ``cd $DIR``, a missing folder, one outside the repository)."""
        cwd = doc.bases if cwd is None else cwd
        for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", _PROMPT.sub("", m.value)):
            part = re.sub(r"\s+#.*$", "", part).strip()
            if not part:
                continue
            try:
                toks = shlex.split(part, posix=True)
            except ValueError:
                toks = part.split()
            while toks and (re.match(r"^[A-Za-z_]\w*=", toks[0]) or toks[0] in ("sudo", "time", "env", "exec")):
                toks = toks[1:]
            if not toks:
                continue
            if toks[0] in ("cd", "pushd", "Set-Location", "sl", "chdir"):
                cwd = self.cd(doc, m, toks[1:], cwd)
                continue
            if toks[0] == "popd":
                cwd = []
                continue
            self.simple(doc, m, toks, cwd)
        return cwd

    def cd(self, doc: Doc, m: Mention, args: list[str], cwd: list[Path]) -> list[Path]:
        args = [a for a in args if not a.startswith("-") or a == "-"]
        if not args or not cwd:
            return []
        d = args[0].replace("\\", "/")
        if self.path_like(d, "arg") or d in self.t.dirs():
            self.check_path(doc, m, d, bases=cwd)
        if d.startswith(("~", "$", "%")) or d == "-" or re.match(r"^[A-Za-z]:/", d) or d.startswith("/"):
            rel = self.t.rel_of(Path(d)) if (re.match(r"^[A-Za-z]:/", d) and os.name == "nt") else None
            return [self.t.repo / rel] if rel is not None and (self.t.repo / rel).is_dir() else []
        for b in cwd:
            p, _case = self.t.locate(b, d.rstrip("/"))
            if p is not None and p.is_dir() and self.t.rel_of(p) is not None:
                return [p]
        return []

    def unfollowed(self, doc: Doc, m: Mention, kind: str, name: str) -> None:
        self.add(Check(kind, name, doc, m.line, "unknown", "unknown",
                       "not checked: it runs in a folder an earlier `cd` moved to, which is not followed"))

    def simple(self, doc: Doc, m: Mention, toks: list[str], cwd: list[Path]) -> None:
        w = toks[0].replace("\\", "/")
        base = PurePosixPath(w).name.lower().removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
        rest = toks[1:]
        known = True
        if base in ("uv", "poetry", "pdm", "hatch", "pipenv", "rye") and rest[:1] == ["run"]:
            if base in PY_MANAGERS:
                self.managers.append((base, doc, m.line))
            args, skip = rest[1:], None
            while args and (skip or args[0].startswith("-")):
                a, args = args[0], args[1:]
                if skip:
                    if skip == "--extra":
                        self.extra(doc, m, a)
                    elif skip in ("--group", "--only-group"):
                        self.group(doc, m, a)
                    elif skip in ("--directory", "--project") and cwd:
                        cwd = [b / a.replace("\\", "/") for b in cwd]
                    skip = None
                elif a.split("=")[0] in _RUN_VALUE_FLAGS:
                    if "=" in a:
                        args.insert(0, a.split("=", 1)[1])
                    skip = a.split("=")[0]
            if args:
                self.simple(doc, m, args, cwd)
            return
        if base in JS_MANAGERS:
            self.js(doc, m, base, rest, cwd)
        elif base in ("npx", "bunx"):
            args = [a for a in rest if not a.startswith("-")]
            if args:
                self.tool(doc, m, args[0], "npm")
        elif re.fullmatch(r"python[\d.]*|py", base):
            self.python(doc, m, rest, cwd)
        elif base in ("pip", "pip3"):
            self.pip(doc, m, rest)
        elif base in PY_MANAGERS:
            if rest[:1] and rest[0] in ("sync", "add", "remove", "lock", "install", "pip", "update"):
                self.managers.append((base, doc, m.line))   # not `uv tool install`: a tool of the user's
            if base == "uv" and rest[:1] == ["pip"]:
                self.pip(doc, m, rest[1:])
            elif rest[:1] == ["add"]:
                self.pip(doc, m, ["install", *rest[1:]])
            elif base == "uv" and rest[:1] == ["sync"]:
                self.uv_sync(doc, m, rest[1:])
        elif base == "make":
            self.make(doc, m, rest, cwd)
        elif base == "just":
            self.just(doc, m, rest, cwd)
        elif base in ("gradlew", "gradle", "mvn", "mvnw"):
            if w.startswith(("./", "../")) or "/" in w:
                self.check_path(doc, m, w, bases=cwd)
            tasks = [a for a in rest if not a.startswith("-")]
            tool = "mvn" if base.startswith("mvn") else "gradle"
            for tk in tasks:
                role = _name_role(tk.split(":")[-1])
                if role:
                    self.roles.append((role, f"{tool}:{tk.split(':')[-1]}", doc, m.line))
            if tasks:
                self.add(Check("task", " ".join(tasks[:4]), doc, m.line, "unknown", "unknown",
                               "Gradle tasks and Maven goals are not read (a build script defines them in code)"))
        elif base in PY_TOOLS or base in JS_TOOLS:
            self.tool(doc, m, base, "python" if base in PY_TOOLS else "npm", rest)
        elif base in self.t.own_scripts() and "/" not in w:
            self.add(Check("command", base, doc, m.line, "ok", VERIFIED, "a console script the project declares",
                           [self.t.own_scripts()[base]]))
        elif base in ("go", "cargo", "dotnet") and rest[:1] and rest[0] in ("test", "build"):
            self.roles.append((rest[0], f"{base} {rest[0]}", doc, m.line))
        elif w.startswith(("./", "../", "~/")) or (("/" in w or w.endswith((".sh", ".ps1", ".py", ".bat")))
                                                   and self.path_like(w, "arg")):
            self.check_path(doc, m, w, bases=cwd)
        else:
            known = False
        if not known and not m.shell:
            return   # an unknown first word in a plain block (a tree diagram, output): its words are not paths
        for a in rest:
            if not a.startswith("-") and "=" not in a and self.path_like(a, "arg"):
                self.check_path(doc, m, a, bases=cwd)

    def js(self, doc: Doc, m: Mention, mgr: str, rest: list[str], cwd: list[Path]) -> None:
        self.managers.append((mgr, doc, m.line))
        args: list[str] = []
        anywhere = False
        skip = False
        for a in rest:
            if skip:
                skip = False
                continue
            if a.split("=")[0] in _PM_VALUE_FLAGS:
                anywhere = True
                skip = "=" not in a
                continue
            if a in _PM_ALL_FLAGS:
                anywhere = True
                continue
            if a.startswith("-"):
                continue
            args.append(a)
        if not args:
            return   # a bare `yarn` / `pnpm`: install
        sub = args[0]
        if mgr == "bun" and sub in _BUN_BUILTINS:
            if sub in ("test", "build"):   # bun's own test runner and bundler, not a package.json script
                self.roles.append((sub, f"bun {sub}", doc, m.line))
            return
        if sub in ("run", "run-script", "rr"):
            if len(args) > 1:
                if mgr == "bun" and self.path_like(args[1], "arg"):
                    self.check_path(doc, m, args[1], bases=cwd)
                else:
                    self.script(doc, m, mgr, args[1], cwd, anywhere)
        elif sub in ("test", "t", "tst", "start", "stop", "restart"):
            self.script(doc, m, mgr, {"t": "test", "tst": "test"}.get(sub, sub), cwd, anywhere)
        elif sub in ("install", "i", "add") and len(args) > 1:
            for name in args[1:]:
                self.package(doc, m, name, "npm")
        elif sub in ("exec", "dlx", "x") and len(args) > 1:
            self.tool(doc, m, args[1], "npm")
        elif mgr != "npm" and sub not in _PM_BUILTINS:
            self.script(doc, m, mgr, sub, cwd, anywhere)

    def script(self, doc: Doc, m: Mention, mgr: str, name: str, cwd: list[Path], anywhere: bool) -> None:
        role = _name_role(name)
        rels: list[str] = []
        if anywhere:
            rels = self.t.package_jsons()
        elif not cwd:
            self.unfollowed(doc, m, "script", f"{mgr} {name}")
            return
        else:
            for b in cwd:
                d = self.t.rel_of(b)
                if d is None:
                    continue
                rel = "package.json" if d == "" else f"{d}/package.json"
                if self.t.memo(f"isfile:{rel}", (self.t.repo / rel).is_file):
                    rels = [rel]
                    break
        if not rels:
            self.add(Check("script", f"{mgr} {name}", doc, m.line, "wrong", VERIFIED,
                           "no package.json where it runs" + ("" if anywhere else " (the folder of the file, or the "
                                                                                    "repository root)")))
            return
        for rel in rels:
            sc = self.t.scripts(rel)
            if name in sc:
                body, ln = sc[name]
                if role:
                    self.roles.append((role, f"script:{name}", doc, m.line))
                    self._expand[(f"script:{name}")] = body
                self.add(Check("script", f"{mgr} {name}", doc, m.line, "ok", VERIFIED, f"script: {body[:80]}",
                               [f"{rel}:{ln}"]))
                return
        known = sorted({k for rel in rels for k in self.t.scripts(rel)})
        self.add(Check("script", f"{mgr} {name}", doc, m.line, "wrong", VERIFIED,
                       f"no script {name!r} in {', '.join(rels[:3])}" + (f" (it has: {', '.join(known[:8])})"
                                                                          if known else " (it has no scripts)"),
                       [f"{rels[0]}:1"], difflib.get_close_matches(name, known, n=3)))

    def make(self, doc: Doc, m: Mention, rest: list[str], cwd: list[Path]) -> None:
        d, mk, targets, skip = None, None, [], None
        for a in rest:
            if skip:
                if skip == "C":
                    d = a
                else:
                    mk = a
                skip = None
            elif a in ("-C", "--directory"):
                skip = "C"
            elif a in ("-f", "--file", "--makefile"):
                skip = "f"
            elif a.startswith("--directory="):
                d = a.split("=", 1)[1]
            elif a.startswith("-") or "=" in a:
                continue
            else:
                targets.append(a)
        bases = [b / d for b in cwd] if d else cwd
        if not bases:
            self.unfollowed(doc, m, "target", "make " + " ".join(targets) if targets else "make")
            return
        rel = None
        for b in bases:
            for n in ([mk] if mk else ["GNUmakefile", "makefile", "Makefile"]):
                p, _case = self.t.locate(b, n)
                if p is not None and p.is_file():
                    rel = _shown(p, self.t.repo, self.t.home)
                    break
            if rel:
                break
        name = "make " + " ".join(targets) if targets else "make"
        if rel is None:
            self.add(Check("target", name, doc, m.line, "wrong", VERIFIED,
                           f"no {mk or 'Makefile'} where it runs"))
            return
        tg, open_ended = self.t.make_targets(rel)
        if not targets:
            self.add(Check("target", name, doc, m.line, "ok", VERIFIED, "the Makefile's default target",
                           [f"{rel}:1"]))
            return
        for t in targets:
            if t in tg:
                role = _name_role(t)
                if role:
                    self.roles.append((role, f"make:{t}", doc, m.line))
                    self._expand[f"make:{t}"] = tg[t][1]
                self.add(Check("target", f"make {t}", doc, m.line, "ok", VERIFIED, "a target of the Makefile",
                               [f"{rel}:{tg[t][0]}"]))
            elif open_ended:
                self.add(Check("target", f"make {t}", doc, m.line, "unknown", "unknown",
                               f"not a target written in {rel}, which includes other files or has pattern rules"))
            else:
                self.add(Check("target", f"make {t}", doc, m.line, "wrong", VERIFIED, f"no target {t!r} in {rel}",
                               [f"{rel}:1"], difflib.get_close_matches(t, list(tg), n=3)))

    def just(self, doc: Doc, m: Mention, rest: list[str], cwd: list[Path]) -> None:
        args = [a for a in rest if not a.startswith("-")]
        if not cwd:
            self.unfollowed(doc, m, "target", ("just " + (args[0] if args else "")).strip())
            return
        rel = None
        for b in cwd:
            for n in ("justfile", "Justfile", ".justfile"):
                p, _case = self.t.locate(b, n)
                if p is not None and p.is_file():
                    rel = _shown(p, self.t.repo, self.t.home)
                    break
            if rel:
                break
        name = "just " + (args[0] if args else "")
        if rel is None:
            self.add(Check("target", name.strip(), doc, m.line, "wrong", VERIFIED, "no justfile where it runs"))
            return
        if not args:
            return
        rc, open_ended = self.t.just_recipes(rel)
        r = args[0]
        if r in rc:
            role = _name_role(r)
            if role:
                self.roles.append((role, f"just:{r}", doc, m.line))
            self.add(Check("target", name, doc, m.line, "ok", VERIFIED, "a recipe of the justfile", [f"{rel}:{rc[r]}"]))
        elif open_ended:
            self.add(Check("target", name, doc, m.line, "unknown", "unknown",
                           f"not a recipe written in {rel}, which imports other files"))
        else:
            self.add(Check("target", name, doc, m.line, "wrong", VERIFIED, f"no recipe {r!r} in {rel}", [f"{rel}:1"],
                           difflib.get_close_matches(r, list(rc), n=3)))

    def python(self, doc: Doc, m: Mention, rest: list[str], cwd: list[Path]) -> None:
        i = 0
        while i < len(rest) and rest[i].startswith("-") and rest[i] != "-m":
            i += 2 if rest[i] in ("-W", "-X") else 1
        rest = rest[i:]
        if rest[:1] == ["-m"] and len(rest) > 1:
            mod, tail = rest[1], rest[2:]
            if mod == "pip":
                self.pip(doc, m, tail)
                return
            if mod in TOOL_ROLES:
                self.roles.append((TOOL_ROLES[mod], mod, doc, m.line))
            if mod == "build":
                self.roles.append(("build", "python -m build", doc, m.line))
            self.module(doc, m, mod)
            for a in tail:
                if not a.startswith("-") and "=" not in a and self.path_like(a, "arg"):
                    self.check_path(doc, m, a, bases=cwd)
        elif rest and not rest[0].startswith("-"):
            if self.path_like(rest[0], "arg"):
                self.check_path(doc, m, rest[0], bases=cwd)
            for a in rest[1:]:
                if not a.startswith("-") and "=" not in a and self.path_like(a, "arg"):
                    self.check_path(doc, m, a, bases=cwd)

    def module(self, doc: Doc, m: Mention, mod: str) -> None:
        top = mod.split(".")[0]
        if top in getattr(sys, "stdlib_module_names", ()) or top in ("pip",):
            self.add(Check("module", mod, doc, m.line, "ok", VERIFIED, "the standard library"))
            return
        parts = mod.split(".")
        for root in (self.t.repo, self.t.repo / "src"):
            p, _case = self.t.locate(root, top)
            q, _case2 = self.t.locate(root, top + ".py")
            if p is None and q is None:
                continue
            sub = "/".join(parts)
            for cand in (sub + ".py", sub + "/__init__.py", sub + "/__main__.py", sub):
                f, _c = self.t.locate(root, cand)
                if f is not None:
                    self.add(Check("module", mod, doc, m.line, "ok", VERIFIED, "a module of the project",
                                   [f"{_shown(f, self.t.repo, self.t.home)}:1"] if f.is_file() else []))
                    return
            self.add(Check("module", mod, doc, m.line, "wrong", VERIFIED,
                           f"the project has {top} but no {sub.replace('/', '.')} module in it"))
            return
        deps = self.t.python_deps()
        key = _dep_key(_DIST_OF.get(top, top))
        if key in deps:
            self.add(Check("module", mod, doc, m.line, "ok", INFERRED, "from a declared package", [deps[key]]))
        elif key in self.t.own_names():
            self.add(Check("module", mod, doc, m.line, "ok", INFERRED, "the project itself"))
        elif not self.t.has_python_manifest():
            self.add(Check("module", mod, doc, m.line, "unknown", "unknown",
                           "not a module of the project or the standard library; no Python manifest to declare it"))
        else:
            self.add(Check("module", mod, doc, m.line, "wrong", INFERRED,
                           "not a module of the project, not the standard library, and no declared package provides "
                           "it (by its name)"))

    def pip(self, doc: Doc, m: Mention, rest: list[str]) -> None:
        if not rest or rest[0] not in ("install", "add"):
            return
        args, skip = rest[1:], None
        for a in args:
            if skip:
                if skip in ("-r", "--requirement", "-c", "--constraint"):
                    self.check_path(doc, m, a, bases=doc.bases)
                elif skip in ("-e", "--editable"):
                    self._pip_target(doc, m, a)
                skip = None
                continue
            if a in ("-r", "--requirement", "-c", "--constraint", "-e", "--editable", "--group", "-i",
                     "--index-url", "--extra-index-url", "-t", "--target", "--python", "-p"):
                skip = a
                continue
            if a.startswith("-"):
                continue
            self._pip_target(doc, m, a)

    def _pip_target(self, doc: Doc, m: Mention, a: str) -> None:
        if "://" in a or a.startswith(("git+", "file:")):
            return
        mm = re.match(r"^(\.{1,2}(?:/[\w.-]+)*|[\w./-]+/)?\s*(?:\[([^\]]*)\])?$", a)
        if mm and mm.group(1) is not None:   # a local project, perhaps with extras: . / .[dev] / ./sub[x]
            if mm.group(1) not in (".", "./"):
                self.check_path(doc, m, mm.group(1), bases=doc.bases)
            for ex in [e.strip() for e in (mm.group(2) or "").split(",") if e.strip()]:
                self.extra(doc, m, ex)
            return
        name = re.split(r"[\[<>=!~;@ ]", a, maxsplit=1)[0]
        if name:
            self.package(doc, m, name, "python")

    def uv_sync(self, doc: Doc, m: Mention, rest: list[str]) -> None:
        skip = None
        for a in rest:
            if skip:
                self.extra(doc, m, a) if skip == "--extra" else self.group(doc, m, a)
                skip = None
            elif a in ("--extra", "--group"):
                skip = a
            elif a.startswith(("--extra=", "--group=")):
                k, v = a.split("=", 1)
                self.extra(doc, m, v) if k == "--extra" else self.group(doc, m, v)

    def extra(self, doc: Doc, m: Mention, name: str) -> None:
        opt = (self.t.pyproject().get("project") or {}).get("optional-dependencies") or {}
        pp = self.t.repo / "pyproject.toml"
        if name in opt:
            self.add(Check("extra", name, doc, m.line, "ok", VERIFIED, "an extra the project declares",
                           [f"pyproject.toml:{_line_of(pp, _key_rx(name))}"]))
        else:
            self.add(Check("extra", name, doc, m.line, "wrong", VERIFIED,
                           "no such extra in pyproject.toml [project.optional-dependencies]" +
                           (f" (it has: {', '.join(list(opt)[:10])})" if opt else ""),
                           ["pyproject.toml:1"] if pp.is_file() else [],
                           difflib.get_close_matches(name, list(opt), n=3)))

    def group(self, doc: Doc, m: Mention, name: str) -> None:
        groups = self.t.pyproject().get("dependency-groups") or {}
        pp = self.t.repo / "pyproject.toml"
        if name in groups:
            self.add(Check("extra", name, doc, m.line, "ok", VERIFIED, "a dependency group the project declares",
                           [f"pyproject.toml:{_line_of(pp, _key_rx(name))}"]))
        else:
            self.add(Check("extra", name, doc, m.line, "wrong", VERIFIED, "no such group in [dependency-groups]",
                           nearest=difflib.get_close_matches(name, list(groups), n=3)))

    def package(self, doc: Doc, m: Mention, name: str, eco: str) -> None:
        if eco == "npm":
            name = re.sub(r"(?<=.)@[^/]*$", "", name)   # a version: pkg@1.2, @scope/pkg@^3
            deps = self.t.npm_deps()
            key = name.lower()
            has_manifest = bool(self.t.package_jsons())
        else:
            deps = self.t.python_deps()
            key = _dep_key(name)
            has_manifest = self.t.has_python_manifest()
        if not key or key in self.t.own_names() or key in {_dep_key(n) for n in self.t.own_names()}:
            return
        if key in deps:
            self.add(Check("package", name, doc, m.line, "ok", VERIFIED, "declared", [deps[key]]))
        elif not has_manifest:
            self.add(Check("package", name, doc, m.line, "unknown", "unknown",
                           f"no {'package.json' if eco == 'npm' else 'Python manifest'} to declare it"))
        else:
            self.add(Check("package", name, doc, m.line, "wrong", VERIFIED,
                           f"installed by hand, not declared in the project's "
                           f"{'package.json files' if eco == 'npm' else 'Python manifests'}",
                           nearest=difflib.get_close_matches(key, list(deps), n=3)))

    def tool(self, doc: Doc, m: Mention, name: str, eco: str, rest: list[str] | None = None) -> None:
        """A tool run by name (``pytest``, ``npx eslint``): the package that provides it is declared."""
        name = re.sub(r"(?<=.)@[^/]*$", "", name)
        role_key = "ruff format" if name == "ruff" and (rest or [])[:1] == ["format"] else name
        if role_key in TOOL_ROLES:
            self.roles.append((TOOL_ROLES[role_key], role_key, doc, m.line))
        provides = (PY_TOOLS.get(name) or JS_TOOLS.get(name) or (name,))
        deps = {**self.t.python_deps(), **self.t.npm_deps()}
        hit = next((deps[k] for p in provides for k in (_dep_key(p), p.lower()) if k in deps), None)
        has_manifest = self.t.has_python_manifest() if eco == "python" else bool(self.t.package_jsons())
        what = " or ".join(provides)
        if hit:
            self.add(Check("tool", name, doc, m.line, "ok", INFERRED, f"{what} is declared", [hit]))
        elif name in self.t.own_scripts():
            self.add(Check("tool", name, doc, m.line, "ok", VERIFIED, "a console script the project declares",
                           [self.t.own_scripts()[name]]))
        elif not has_manifest:
            self.add(Check("tool", name, doc, m.line, "unknown", "unknown",
                           f"no {'package.json' if eco == 'npm' else 'Python manifest'} to declare it"))
        else:
            self.add(Check("tool", name, doc, m.line, "wrong", INFERRED,
                           f"{what} is not declared in the project's "
                           f"{'package.json files' if eco == 'npm' else 'Python manifests'}"
                           + (" (npx would download it)" if eco == "npm" else "")))

    # the files agree
    def agreement(self) -> None:
        by_role: dict[str, dict[str, list[tuple[str, int]]]] = {}
        docs: dict[str, Doc] = {}
        for role, key, doc, line in self.roles:
            docs[doc.rel] = doc
            by_role.setdefault(role, {}).setdefault(doc.rel, []).append((key, line))
        for role, per in sorted(by_role.items()):
            names = sorted(per)
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    if docs[a].scope != docs[b].scope:
                        continue   # a nested file governs its own folder: another stack may run other commands
                    ka, kb = self._keys(per[a]), self._keys(per[b])
                    la, lb = per[a][0][1], per[b][0][1]
                    shown_a = ", ".join(dict.fromkeys(k for k, _ in per[a]))
                    shown_b = ", ".join(dict.fromkeys(k for k, _ in per[b]))
                    if ka & kb:
                        self.add(Check("agreement", f"{role}: {a} / {b}", docs[a], la, "ok", INFERRED,
                                       f"both run {', '.join(sorted(ka & kb))}", [f"{b}:{lb}"]))
                    else:
                        self.add(Check("agreement", f"{role}: {a} / {b}", docs[a], la, "wrong", INFERRED,
                                       f"{a} runs {role} with {shown_a}; {b} with {shown_b}", [f"{b}:{lb}"]))
        locks = self.t.locks()
        for family in (JS_MANAGERS, PY_MANAGERS):
            used: dict[str, dict[str, int]] = {}
            for mgr, doc, line in self.managers:
                if mgr in family:
                    docs[doc.rel] = doc
                    used.setdefault(doc.rel, {}).setdefault(mgr, line)
            locked = [x for x in family if x in locks]
            if len(locked) == 1:
                lk = locked[0]
                for rel, mgrs in sorted(used.items()):
                    for mgr, line in mgrs.items():
                        ok = mgr == lk
                        self.add(Check("agreement", f"package manager {mgr}", docs[rel], line,
                                       "ok" if ok else "wrong", VERIFIED if ok else INFERRED,
                                       f"the repository pins {lk} ({locks[lk].rsplit(':', 1)[0]})", [locks[lk]]))
            elif not locked:
                only = {rel: next(iter(mg)) for rel, mg in used.items() if len(mg) == 1}
                rels = sorted(only)
                for i, a in enumerate(rels):
                    for b in rels[i + 1:]:
                        if only[a] != only[b] and docs[a].scope == docs[b].scope:
                            self.add(Check("agreement", f"package manager: {a} / {b}", docs[a], used[a][only[a]],
                                           "wrong", INFERRED, f"{a} uses {only[a]}; {b} uses {only[b]} (no lock file "
                                                              f"says which)", [f"{b}:{used[b][only[b]]}"]))

    def _keys(self, pairs: list[tuple[str, int]]) -> set[str]:
        """A role's commands, and the tools the scripts and targets among them run."""
        out = set()
        for k, _ln in pairs:
            out.add(k)
            body = self._expand.get(k, "")
            for tool in TOOL_ROLES:
                if re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", body):
                    out.add(tool)
            for mm in re.finditer(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?([\w:-]+)", body):
                out.add(f"script:{mm.group(1)}")
            for mm in re.finditer(r"\bmake\s+([\w-]+)", body):
                out.add(f"make:{mm.group(1)}")
        return out


def _name_role(name: str) -> str | None:
    n = name.lower()
    return next((role for role, rx in _ROLE_NAMES if re.fullmatch(rx, n)), None)


def _run(tree: Tree, docs: list[Doc]) -> Linter:
    lt = Linter(tree)
    for doc in docs:
        block_cwd: dict[int, list[Path]] = {}
        for m in mentions(doc):
            if m.how == "fence":
                block_cwd[m.block] = lt.command(doc, m, block_cwd.get(m.block))
            elif m.how == "command" and lt.names_existing_path(doc, m.value):
                lt.check_path(doc, m, m.value)   # a path with a space in it, not a command
            elif m.how == "command":
                lt.command(doc, m)
            elif m.how == "link":
                v = m.value.split("#")[0]
                if v and (re.match(r"^[A-Za-z]:[\\/]", v) or not re.match(r"^[a-z][\w+.-]*:", v, re.I)):
                    from urllib.parse import unquote

                    lt.check_path(doc, m, unquote(v), "link", bases=[doc.link_base])
            elif m.how == "import":
                if lt.path_like(m.value, "import"):
                    lt.check_path(doc, m, m.value, "import", bases=[doc.link_base] + doc.bases)
            elif m.how == "code":
                w = m.value.split()[0] if m.value.split() else ""
                base = PurePosixPath(w.replace("\\", "/")).name.lower()
                if base in PY_TOOLS or base in JS_TOOLS or base in JS_MANAGERS or base == "make":
                    lt.command(doc, m)
                elif lt.path_like(m.value, "code"):
                    lt.check_path(doc, m, m.value)
            elif m.how == "prose" and lt.path_like(m.value, "prose"):
                lt.check_path(doc, m, m.value)
    lt.settle_missing()
    lt.agreement()
    return lt


ORDER = {"wrong": 0, "unknown": 1, "ok": 2}


def lint(repo: Path, *, extra: list[str] | None = None, memory: bool = True, include_ok: bool = False,
         home: Path | None = None, files: list[str] | None = None, max_checks: int = MAX_CHECKS) -> dict:
    """``verinoda agent-lint``: every line of the agent instruction files that names something, checked."""
    from verinoda.snapshot import list_files

    repo = Path(repo).resolve()
    home = Path.home() if home is None else Path(home)
    files = list_files(repo) if files is None else files
    docs = instruction_files(repo, files, extra=extra, memory=memory, home=home)
    limits = [
        "read from the text: a path, script, target, module or package is checked where a line names it (a code span, "
        "a link, an @import, a shell block, a prose path with a folder); a name in plain words is not",
        "a path is resolved from the file's folder, then the repository root (a memory file: the repository first; "
        "a link: its own folder); the case must match as written",
        "Gradle tasks and Maven goals are unknown; the subcommands of the project's own console scripts are not "
        "checked",
        "which package provides a tool, and whether two commands for one role disagree, are strong_inference: a "
        "script or target that runs the other file's tool counts as agreeing",
        "a path outside the repository (an absolute path, ~/... outside a memory file) is unknown; /x in a link, or "
        "whose first folder the repository has, is read from the repository root",
        "`cd` carries to the next lines of the same shell block; a folder it cannot follow (cd -, cd $DIR, a "
        "missing one) makes what runs there unknown",
        "a nested instruction file governs its folder: its commands are compared only with files of the same folder",
        "Verinoda's own marked block (`verinoda install` adds it to GEMINI.md and Copilot's instructions) is not read",
    ]
    if not docs:
        return {"status": "no_files", "files": [], "summary": {"checks": 0, "ok": 0, "wrong": 0, "unknown": 0},
                "checks": [], "note": "no AGENTS.md, CLAUDE.md, GEMINI.md, .github/copilot-instructions.md, Cursor, "
                                      "Windsurf or Cline rules, and no Claude Code memory for this project",
                "limits": limits}
    lt = _run(Tree(repo, files, home), docs)
    recs = [c.record() for c in lt.checks]
    recs.sort(key=lambda r: (ORDER[r["verdict"]], r["at"].rsplit(":", 1)[0],
                             int(r["at"].rsplit(":", 1)[1]) if r["at"].rsplit(":", 1)[1].isdigit() else 0))
    counts = {v: sum(1 for r in recs if r["verdict"] == v) for v in ("ok", "wrong", "unknown")}
    per = []
    for doc in docs:
        mine = [c for c in lt.checks if c.doc is doc]
        per.append({"path": doc.rel, "kind": doc.kind, "lines": len(doc.lines), "checks": len(mine),
                    "wrong": sum(1 for c in mine if c.verdict == "wrong"),
                    "unknown": sum(1 for c in mine if c.verdict == "unknown")})
    shown = recs if include_ok else [r for r in recs if r["verdict"] != "ok"]
    cut = len(shown) > max_checks
    return {"status": "wrong" if counts["wrong"] else "ok", "files": per,
            "summary": {"checks": len(recs), **counts}, "checks": shown[:max_checks],
            **({"truncated": True, "checks_not_listed": len(shown) - max_checks} if cut else {}),
            **({"ok_not_listed": counts["ok"]} if not include_ok and counts["ok"] else {}),
            "limits": limits}


def render(res: dict) -> str:
    s = res["summary"]
    if res["status"] == "no_files":
        return f"agent-lint: {res.get('note')}"
    out = [f"agent-lint: {len(res['files'])} file(s), {s['checks']} check(s): {s['ok']} ok, {s['wrong']} wrong, "
           f"{s['unknown']} unknown"]
    for f in res["files"]:
        out.append(f"  {f['path']} ({f['kind']}): {f['checks']} checks" +
                   (f", {f['wrong']} wrong" if f["wrong"] else "") +
                   (f", {f['unknown']} unknown" if f["unknown"] else ""))
    for verdict in ("wrong", "unknown", "ok"):
        rows = [r for r in res["checks"] if r["verdict"] == verdict]
        if rows:
            out.append(f"{verdict}:")
        for r in rows:
            out.append(f"  {r['at']}  {r['kind']} {r['name']}: {r['why']}" +
                       (f" (nearest: {', '.join(r['nearest'])})" if r.get("nearest") else "") +
                       (f" [{r['status']}]" if r["status"] not in (VERIFIED, "unknown") else ""))
            if verdict != "ok":
                out.append(f"      > {r['text']}")
    if res.get("truncated"):
        out.append(f"({res['checks_not_listed']} more not listed)")
    if res.get("ok_not_listed"):
        out.append(f"({res['ok_not_listed']} ok not listed: --all)")
    return "\n".join(out)
