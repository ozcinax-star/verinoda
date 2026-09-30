"""Declared dependencies against the imports that use them (``verinoda check --deps``, MCP ``code_check``
with ``deps``).

The manifests are the ones the dependency guards read (:func:`verinoda.guards.declared_dependencies`:
pyproject.toml, requirements*.txt, setup.py/cfg, package.json with its workspace packages, the Gradle and
Maven builds the root build includes); PEP 735 ``[dependency-groups]`` and Poetry groups are added here as
dev groups. The imports come from the project's own files (the file list of the snapshot, so git-ignored
files are not read; sample, fixture, benchmark-corpus, vendor and build-output folders, reference trees
and detected copies are left out). Nothing is run or installed; nothing leaves the machine.

Four findings, each a claim with its status and ``file:line`` evidence (the import lines, the declaration
line, the lock-file line):

* ``missing`` - code imports a package no manifest declares, and nothing shows it installed as another
  package's dependency;
* ``transitive_only`` - code imports a package no manifest declares that is installed only because a
  declared package requires it (Python: the ``Requires-Dist`` of the installed metadata; npm: the lock file or
  ``node_modules``);
* ``unused`` - a runtime dependency no file imports (dev groups are not judged: tools such as pytest or eslint
  are run, not imported);
* ``wrong_group`` - a dependency declared only in a dev group that runtime code imports, or a runtime
  dependency only test and documentation code imports.

What counts as dev code: test files (:func:`verinoda.testcode.is_test_file`), ``docs/`` and ``benchmarks/``,
build and task scripts (``setup.py``, ``noxfile.py``, ``tasks.py``) and JS/TS tool configuration
(``*.config.js``, stories, ``e2e/``). A dev group is a PEP 735 group, a Poetry group, npm
``devDependencies``, a ``requirements-dev``-like file, an optional-dependencies group named like dev, test,
lint, docs or typing, a Gradle ``test*`` configuration or a Maven ``test`` scope.

How an import is tied to a package, and what that makes the status:

* Python with a project environment (``.venv``, ``venv`` or ``env``; read from files, its interpreter never
  started): the installed distribution whose ``RECORD``/``top_level.txt`` holds the module. ``missing`` and
  ``transitive_only`` are ``statically_verified`` when every manifest of the project was read;
* Python without one: the import name against the declared names (normalised, a table of well-known
  differences such as ``yaml`` for PyYAML): ``strong_inference`` at most, and a name matching nothing may be
  a package installed some other way;
* npm: the specifier names the package; a bundler alias or a tsconfig path is resolved first
  (:class:`verinoda.codecheck_ts.Resolver`). ``transitive_only`` is ``statically_verified`` (the lock file or
  ``node_modules`` holds it), ``missing`` ``strong_inference`` (a bundler may still provide it);
* Gradle and Maven: a Java or Kotlin import is tied to a declared artifact by its group (``org.yaml`` for
  ``org.yaml.snakeyaml``), its artifact name as a package segment, or a table of well-known packages (Guava's
  ``com.google.common``): ``strong_inference``. Without reading the classpath an import that matches nothing
  may come from the JDK, the platform (Minecraft), or a transitive jar, so JVM imports are never ``missing``
  or ``transitive_only``: their count is a limit.

``unused`` and ``wrong_group`` are ``strong_inference`` everywhere: a package can be used without an import
(a plugin, an entry point, a string passed to ``importlib``) and the dev/runtime split of files is a rule.
An import inside ``try``/``except ImportError`` or ``if TYPE_CHECKING`` (TypeScript: ``import type``) is
optional: it is never ``missing`` and never makes a dev dependency a runtime one.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FINDINGS = ("missing", "transitive_only", "unused", "wrong_group")
EVIDENCE_SITES = 3          # import sites cited per finding (the count says how many there are)
MAX_FILE_BYTES = 2_000_000

# folders whose code is not the project's own (samples, fixtures, vendored or generated code)
_NOT_PROJECT = {"node_modules", "build", "dist", "out", ".gradle", "target", "vendor", "third_party", "third-party",
                "examples", "example", "samples", "sample", "fixtures", "fixture", "__fixtures__", "testdata",
                "test-data", "demo", "demos", "corpora", ".verinoda", ".venv", "venv", "site-packages", "coverage",
                ".next"}
_DEV_DIRS = {"docs", "doc", "benchmarks", "benchmark", "e2e", "cypress", "playwright", "__mocks__", ".storybook",
             "stories"}
_DEV_FILES = {"setup.py", "noxfile.py", "tasks.py", "fabfile.py", "conftest.py", "gulpfile.js", "gruntfile.js",
              "Gruntfile.js", "Gulpfile.js"}
_DEV_NAME = re.compile(r"(^|[._-])(config|conf|rc|stories|story)\.[cm]?[jt]sx?$|^\.[\w-]+rc\.[cm]?js$")
_DEV_GROUP = re.compile(r"dev|test|lint|doc|typ|check|ci\b|style|format|bench|coverage", re.I)

# import name -> distribution, where they differ and no environment says so
PY_ALIASES = {
    "yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python", "sklearn": "scikit-learn", "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil", "dotenv": "python-dotenv", "jwt": "pyjwt", "git": "gitpython",
    "OpenSSL": "pyopenssl", "Crypto": "pycryptodome", "attr": "attrs", "magic": "python-magic",
    "serial": "pyserial", "usb": "pyusb", "docx": "python-docx", "pptx": "python-pptx", "MySQLdb": "mysqlclient",
    "google.protobuf": "protobuf", "multipart": "python-multipart", "jose": "python-jose", "slugify": "python-slugify",
    "zmq": "pyzmq", "win32api": "pywin32", "win32con": "pywin32", "pythoncom": "pywin32", "fitz": "pymupdf",
    "Levenshtein": "python-levenshtein", "skimage": "scikit-image", "wx": "wxpython", "gi": "pygobject",
    "tomli_w": "tomli-w", "mpl_toolkits": "matplotlib", "pkg_resources": "setuptools", "_pytest": "pytest",
}
# declared Python packages that are not imported by design (build back-ends, stubs, installers)
_PY_NOT_IMPORTED = re.compile(r"^(types-.+|.+-stubs|setuptools|wheel|pip|build|hatchling|flit-core|poetry-core|"
                              r"uv|twine|tox|nox|pre-commit)$")
# JVM groups whose packages differ from the group and artifact names
JVM_ALIASES = {
    "com.google.guava:guava": ("com.google.common", "com.google.thirdparty"),
    "com.fasterxml.jackson.core:jackson-databind": ("com.fasterxml.jackson.databind",),
    "com.fasterxml.jackson.core:jackson-annotations": ("com.fasterxml.jackson.annotation",),
    "junit:junit": ("org.junit", "junit."),
    "com.google.code.gson:gson": ("com.google.gson",),
    "com.google.code.findbugs:jsr305": ("javax.annotation",),
    "org.jetbrains.kotlinx:kotlinx-coroutines-core": ("kotlinx.coroutines",),
    "org.jetbrains.kotlinx:kotlinx-serialization-json": ("kotlinx.serialization",),
    "commons-io:commons-io": ("org.apache.commons.io",),
    "commons-codec:commons-codec": ("org.apache.commons.codec",),
    "org.projectlombok:lombok": ("lombok",),
    "org.assertj:assertj-core": ("org.assertj",),
}
_JVM_PLATFORM = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "kotlin.", "kotlinx.", "org.w3c.", "org.xml.",
                 "org.ietf.", "org.omg.")
# configurations that compile code (the others bundle, run or process it: no import is expected)
_JVM_COMPILE = re.compile(r"^(\w*?)(implementation|api|compileOnly|compileOnlyApi|compile|provided|test)$", re.I)


@dataclass
class Use:
    """One import of a package: where, from which kind of file, and whether it is optional."""

    ecosystem: str
    name: str            # the import as written (module, specifier, package)
    path: str
    line: int
    dev: bool
    optional: bool = False


def _dep_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()


def _read(p: Path) -> str | None:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        return p.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def _line_text(repo: Path, rel: str, line: int, cache: dict) -> str:
    if rel not in cache:
        cache[rel] = (_read(repo / rel) or "").split("\n")
    lines = cache[rel]
    return lines[line - 1].strip()[:160] if 0 < line <= len(lines) else ""


def is_dev_file(rel: str) -> bool:
    """Test, documentation, benchmark, build-script and tool-configuration files (see the module docstring)."""
    from verinoda.testcode import is_test_file

    p = PurePosixPath(rel)
    return (is_test_file(rel) or p.name in _DEV_FILES or bool(set(p.parts[:-1]) & _DEV_DIRS)
            or bool(_DEV_NAME.search(p.name)) or ".stories." in p.name)


def _group(item: dict) -> str:
    """``runtime``, ``dev`` or ``optional`` (an extra that is not a dev group: not judged either way)."""
    scope = str(item.get("scope") or "")
    eco = item.get("ecosystem")
    if eco == "npm":
        return {"runtime": "runtime", "dev": "dev"}.get(scope, "optional")
    if eco == "maven":
        if scope.lower() == "test" or scope.lower().startswith(("test", "androidtest")) or "Test" in scope:
            return "dev"
        return "runtime"
    if scope.startswith(("optional:", "group:")):
        grp = scope.split(":", 1)[1]
        return "dev" if scope.startswith("group:") or _DEV_GROUP.search(grp) else "optional"
    return "dev" if scope == "dev" else "runtime"


# -- declarations the guards' reader does not return -----------------------------------------------

def _python_groups(repo: Path) -> list[dict]:
    """PEP 735 ``[dependency-groups]``, Poetry ``[tool.poetry.group.X.dependencies]`` and the old
    ``[tool.poetry.dev-dependencies]``: dev groups, cited at the first line naming the package in the group."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - py3.10
        import tomli as tomllib  # type: ignore[no-redef]

    raw = _read(repo / "pyproject.toml")
    if not raw:
        return []
    try:
        data = tomllib.loads(raw)
    except Exception:  # noqa: BLE001 - an unreadable pyproject: the guards' reader says so
        return []
    lines = raw.split("\n")
    groups: list[tuple[str, str, list[str]]] = []   # (group, section header regex, names)
    for grp, lst in (data.get("dependency-groups") or {}).items():
        names = [re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", s).group(1) for s in lst or []
                 if isinstance(s, str) and re.match(r"\s*[A-Za-z0-9]", s)]
        groups.append((grp, r"\[dependency-groups\]", names))
    poetry = (data.get("tool") or {}).get("poetry") or {}
    for grp, body in (poetry.get("group") or {}).items():
        names = [n for n in ((body or {}).get("dependencies") or {}) if n.lower() != "python"]
        groups.append((grp, rf"\[tool\.poetry\.group\.{re.escape(grp)}\.dependencies\]", names))
    if poetry.get("dev-dependencies"):
        groups.append(("dev", r"\[tool\.poetry\.dev-dependencies\]", list(poetry["dev-dependencies"])))
    out = []
    for grp, header, names in groups:
        start = next((i for i, ln in enumerate(lines) if re.match(rf"\s*{header}", ln)), 0)
        for name in names:
            line = next((i + 1 for i in range(start, len(lines))
                         if re.search(rf"(^|['\"\s]){re.escape(name)}([\s'\"<>=!~;\[,]|$)", lines[i])), start + 1)
            out.append({"name": _dep_key(name), "spec": "*", "scope": f"group:{grp}", "at": f"pyproject.toml:{line}",
                        "path": "pyproject.toml", "line": line, "ecosystem": "python"})
    return out


# -- imports -----------------------------------------------------------------------------------------

def _optional_lines(tree: ast.AST) -> set[int]:
    """Lines of imports that may fail or never run: inside ``try`` with an ``except`` of ImportError (or a
    broader one), and under ``if TYPE_CHECKING``."""
    out: set[int] = set()
    catches = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}

    def names(t) -> set[str]:
        if t is None:
            return {"BaseException"}
        if isinstance(t, ast.Tuple):
            return {n for e in t.elts for n in names(e)}
        return {t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", "")}

    for node in ast.walk(tree):
        body = None
        if isinstance(node, ast.Try) and any(names(h.type) & catches for h in node.handlers):
            body = node.body
        elif isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            body = node.body
        for stmt in body or ():
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    out.add(sub.lineno)
    return out


def python_imports(text: str, rel: str, strings: set[str] | None = None) -> list[Use] | None:
    """The absolute imports of a Python file (None: it does not parse). ``strings`` collects the string
    literals that look like a module name (``importlib.import_module("tree_sitter_lua")``)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    opt = _optional_lines(tree)
    dev = is_dev_file(rel)
    out = []
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            mods = [node.module]
        for m in mods:
            out.append(Use("python", m, rel, node.lineno, dev, node.lineno in opt))
        if strings is not None and isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and _MODULE_NAME.match(node.value):
            strings.add(node.value.split(".")[0])
    return out


_JS_IMPORT = re.compile(
    r"""\bimport\s+(type\s+)?[\w*${}\s,]*?\bfrom\s*(['"])([^'"\n]+)\2"""
    r"""|\bexport\s+(type\s+)?[\w*${}\s,]*?\bfrom\s*(['"])([^'"\n]+)\5"""
    r"""|\bimport\s*(['"])([^'"\n]+)\7"""
    r"""|\b(?:require|import)\s*\(\s*(['"])([^'"\n]+)\9\s*\)""")


def js_imports(text: str, rel: str) -> list[tuple[str, int, bool]]:
    """(specifier, line, type only) of the imports, re-exports, requires and dynamic imports of a JS/TS file."""
    from verinoda.guards import code_text

    code = code_text(text, PurePosixPath(rel).suffix or ".js", keep_strings=True)
    out = []
    for m in _JS_IMPORT.finditer(code):
        spec = m.group(3) or m.group(6) or m.group(8) or m.group(10)
        type_only = bool(m.group(1) or m.group(4))
        out.append((spec, code.count("\n", 0, m.start()) + 1, type_only))
    return out


_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
_JVM_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([A-Za-z_][\w.]*)", re.M)
_JVM_PACKAGE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)", re.M)


# -- Python --------------------------------------------------------------------------------------------

def _venv(repo: Path, env: str | None) -> tuple[dict, dict, str, str]:
    """``(dists, top_level, label, note)`` of the project's virtual environment, read from files (no
    interpreter is started). ``env``: ``auto``, ``none`` or a virtual environment directory."""
    from verinoda.codecheck_env import VENV_DIRS, _dists, explicit_env_path, read_pyvenv, venv_of, venv_site_dirs

    choice = (env or "auto").strip()
    if choice == "none":
        return {}, {}, "", "--env none: imports are tied to packages by name only"
    if choice == "auto":
        cands = [repo / d for d in VENV_DIRS if (repo / d / "pyvenv.cfg").is_file()]
    else:
        v = venv_of(explicit_env_path(repo, choice))
        if v is None:
            raise ValueError(f"env {choice}: not a virtual environment (a directory with pyvenv.cfg)")
        cands = [v]
    for venv in cands:
        cfg = read_pyvenv(venv)
        m = re.match(r"(\d+)\.(\d+)", cfg.get("version_info") or cfg.get("version") or "")
        sites = venv_site_dirs(venv, (int(m.group(1)), int(m.group(2)))) if m else []
        if not sites:
            sites = [p for p in sorted(venv.glob("lib/python*/site-packages")) + [venv / "Lib" / "site-packages"]
                     if p.is_dir()]
        if sites:
            dists, top = _dists(sites)
            try:
                where = venv.relative_to(repo).as_posix()
            except ValueError:
                where = str(venv)
            return dists, top, f"{where} ({len(dists)} installed distributions)", ""
    return {}, {}, "", ("no project environment (.venv, venv or env with pyvenv.cfg): imports are tied to packages "
                        "by name only")


def _requires(dist_info: Path) -> list[str]:
    """The distributions an installed one requires (not those of its extras)."""
    meta = dist_info / ("METADATA" if dist_info.name.endswith(".dist-info") else "PKG-INFO")
    out = []
    for ln in (_read(meta) or "").split("\n"):
        if not ln.startswith("Requires-Dist:"):
            if not ln.strip():
                break   # the headers end at the first blank line
            continue
        req = ln.split(":", 1)[1]
        if re.search(r"\bextra\s*==", req):
            continue
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
        if m:
            out.append(_dep_key(m.group(1)))
    return out


def _closure(dists: dict, roots: set[str]) -> dict[str, str]:
    """Installed distributions the declared ones require, directly or not: {dist: the declared root}."""
    seen: dict[str, str] = {}
    todo = [(r, r) for r in sorted(roots) if r in dists]
    while todo:
        cur, root = todo.pop()
        for dep in _requires(dists[cur][2]):
            if dep not in seen and dep not in roots and dep in dists:
                seen[dep] = root
                todo.append((dep, root))
    return seen


def _own_python_names(repo: Path) -> set[str]:
    """The project's own distribution name (``[project] name``, Poetry's, setup.cfg's ``[metadata] name``)."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - py3.10
        import tomli as tomllib  # type: ignore[no-redef]

    out: set[str] = set()
    try:
        data = tomllib.loads(_read(repo / "pyproject.toml") or "")
    except Exception:  # noqa: BLE001
        data = {}
    for name in ((data.get("project") or {}).get("name"), ((data.get("tool") or {}).get("poetry") or {}).get("name")):
        if isinstance(name, str):
            out.add(_dep_key(name))
    m = re.search(r"^\[metadata\][^\[]*?^\s*name\s*=\s*([A-Za-z0-9._-]+)", _read(repo / "setup.cfg") or "",
                  re.M | re.S)
    if m:
        out.add(_dep_key(m.group(1)))
    return out


def _first_party_python(files: list[str]) -> set[str]:
    """Top-level names the project's own modules can be imported by: the outermost folder of each chain of
    packages (folders with ``__init__.py``), the stem of each module outside a package, and every folder of
    Python files that is not a package (a namespace package, or a folder a script puts on sys.path)."""
    py = [PurePosixPath(f) for f in files if f.endswith((".py", ".pyi"))]
    packages = {p.parent.as_posix() for p in py if p.name in ("__init__.py", "__init__.pyi")}
    out: set[str] = set()
    for p in py:
        d = p.parent
        if d.as_posix() not in packages:
            out.add(p.stem)
            if d.name:
                out.add(d.name)
            continue
        while d.parent.as_posix() in packages:
            d = d.parent
        out.add(d.name)
    return out


def _py_key(module: str, declared: set[str]) -> str:
    """The declared distribution an import names, by name only: the module, a well-known alias, a dotted
    prefix joined with dashes (``google.cloud.storage`` -> google-cloud-storage), or a declared name that
    extends the module's (``psycopg2`` -> psycopg2-binary). Else the module's own normalised top name."""
    parts = module.split(".")
    for i in range(len(parts), 0, -1):
        dotted = ".".join(parts[:i])
        for cand in (PY_ALIASES.get(dotted), _dep_key("-".join(parts[:i]))):
            if cand and _dep_key(cand) in declared:
                return _dep_key(cand)
    top = _dep_key(parts[0])
    ext = sorted(d for d in declared if d.startswith(top + "-") or d == f"python-{top}" or d == f"py{top}")
    if ext:
        return ext[0]
    return _dep_key(PY_ALIASES.get(parts[0]) or parts[0])


# -- the check -------------------------------------------------------------------------------------

class _Out:
    def __init__(self, repo: Path):
        self.repo = repo
        self.findings: list[dict] = []
        self.limits: list[str] = []
        self._lines: dict = {}

    def ev(self, rel: str, line: int, role: str) -> dict:
        return {"locator": f"{rel}:{line}", "role": role, "excerpt": _line_text(self.repo, rel, line, self._lines)}

    def add(self, finding: str, eco: str, package: str, status: str, claim: str, uses: list[Use],
            decls: list[dict] = (), extra: list[dict] = (), next_step: str = "") -> None:
        cited = dict.fromkeys((d["path"], d["line"]) for d in decls)   # one entry per declaration line
        evidence = [self.ev(path, line, "declaration") for path, line in cited]
        evidence += [self.ev(u.path, u.line, "import") for u in uses[:EVIDENCE_SITES]]
        evidence += list(extra)
        self.findings.append({"finding": finding, "ecosystem": eco, "package": package, "status": status,
                              "claim": claim, "evidence": evidence,
                              **({"import_sites": len(uses)} if uses else {}),
                              **({"next_step": next_step} if next_step else {})})


def _in_scope(decl: dict, rel: str) -> bool:
    """Does a declaration of this manifest apply to this file (its folder, or the root's to every file)?"""
    d = PurePosixPath(decl["path"]).parent.as_posix()
    return d in ("", ".") or rel.startswith(d + "/")


def _judge_declared(out: _Out, eco: str, key: str, decls: list[dict], uses: list[Use], label: str) -> None:
    """``wrong_group`` and ``unused`` for one declared package (all its declarations together)."""
    groups = {_group(d) for d in decls}
    hard = [u for u in uses if not u.optional]
    runtime_uses = [u for u in hard if not u.dev]
    if groups == {"dev"} and runtime_uses:
        where = ", ".join(sorted({str(d.get('scope')) for d in decls}))
        out.add("wrong_group", eco, label, "strong_inference",
                f"{label} is declared only as a dev dependency ({where}) but runtime code imports it",
                runtime_uses, decls, next_step=f"move {label} to the runtime dependencies, or the importing code "
                                               "to test or tool code")
        return
    if "runtime" in groups and "dev" not in groups and uses and all(u.dev for u in uses):
        out.add("wrong_group", eco, label, "strong_inference",
                f"{label} is a runtime dependency, but only test, documentation or tool code imports it",
                uses, [d for d in decls if _group(d) == "runtime"],
                next_step=f"move {label} to a dev group if the shipped code does not need it")
        return
    if "runtime" in groups and not uses:
        out.add("unused", eco, label, "strong_inference",
                f"{label} is a runtime dependency no file of the project imports",
                [], [d for d in decls if _group(d) == "runtime"],
                next_step=f"search for {label} used without an import (a plugin, an entry point, a string given to "
                          "an importer) before removing it")


def _python(out: _Out, repo: Path, files: list[str], items: list[dict], env: str | None, all_read: bool) -> dict:
    decls: dict[str, list[dict]] = {}
    for it in items:
        decls.setdefault(_dep_key(it["name"]), []).append(it)
    py = [f for f in files if f.endswith(".py")]
    if not py and not decls:
        return {}
    dists, top, label, note = _venv(repo, env)
    if note:
        out.limits.append(f"python: {note}")
    own = _own_python_names(repo)
    first = _first_party_python(files)
    std = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names) | {"__future__"}
    uses: dict[str, list[Use]] = {}
    by_env: set[str] = set()
    unparsed = []
    strings: set[str] = set()
    for rel in py:
        got = python_imports(_read(repo / rel) or "", rel, strings)
        if got is None:
            unparsed.append(rel)
            continue
        for u in got:
            topname = u.name.split(".")[0]
            if topname in std or topname in first:
                continue
            key = _dep_key(top[topname]) if topname in top else _py_key(u.name, set(decls))
            if topname in top:
                by_env.add(key)
            if key in own:
                continue
            uses.setdefault(key, []).append(u)
    if unparsed:
        out.limits.append(f"python: {len(unparsed)} file(s) do not parse, their imports are not read: "
                          f"{', '.join(unparsed[:5])}{' ...' if len(unparsed) > 5 else ''}")
    closure = _closure(dists, set(decls)) if dists else {}
    for key in sorted(uses):
        if key in decls:
            continue
        hard = [u for u in uses[key] if not u.optional]
        name = dists[key][0] if key in dists else key
        if not hard:
            continue
        exact = key in by_env and all_read
        if key in closure:
            root = closure[key]
            out.add("transitive_only", "python", name, "statically_verified" if exact else "strong_inference",
                    f"{name} is imported but not declared; it is installed because {root} requires it",
                    hard, decls[root][:1], next_step=f"declare {name} if the code relies on it directly")
        else:
            installed = " and installed," if key in dists else ""
            out.add("missing", "python", name, "statically_verified" if exact else "strong_inference",
                    f"{name} is imported{installed} but declared by no manifest" +
                    ("" if key in by_env else " (the import name is tied to the package by name only)"),
                    hard, next_step=f"declare {name} in pyproject.toml (or the requirements file), or check that "
                                    "the import is the project's own module")
    by_name = []
    for key, ds in sorted(decls.items()):
        if _PY_NOT_IMPORTED.match(key) or key in own:
            continue
        modules = {m for m, d in top.items() if _dep_key(d) == key} | {key.replace("-", "_")} | \
                  {m for m, d in PY_ALIASES.items() if _dep_key(d) == key}
        if not uses.get(key) and modules & strings:
            by_name.append(ds[0]["name"])   # imported by a name in a string (importlib): not judged unused
            continue
        _judge_declared(out, "python", key, ds, uses.get(key, []), ds[0]["name"])
    if by_name:
        out.limits.append(f"python: {len(by_name)} declared package(s) are named only in a string literal (a "
                          f"dynamic import), so they are not reported unused: {', '.join(by_name[:5])}"
                          f"{' ...' if len(by_name) > 5 else ''}")
    return {"files": len(py) - len(unparsed), "declared": len(decls), "imported_packages": len(uses),
            **({"environment": label} if label else {})}


# -- npm -----------------------------------------------------------------------------------------------

_NPM_NAME = re.compile(r"^(@[a-z0-9~][a-z0-9._~-]*/)?[a-z0-9~][a-z0-9._~-]*$")
_JS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")


def _lock_line(repo: Path, rels: list[str], pkg: str, cache: dict) -> tuple[str, int] | None:
    """Where a lock file (package-lock.json, yarn.lock, pnpm-lock.yaml) of the project names a package."""
    pats = (rf'"(?:[^"]*/)?node_modules/{re.escape(pkg)}"\s*:', rf'^"?{re.escape(pkg)}@',
            rf"^\s+/?'?{re.escape(pkg)}[@/:]")
    for rel in rels:
        if rel not in cache:
            cache[rel] = (_read(repo / rel) or "").split("\n")
        for i, ln in enumerate(cache[rel], 1):
            if any(re.search(p, ln) for p in pats):
                return rel, i
    return None


def _npm(out: _Out, repo: Path, files: list[str], items: list[dict], all_read: bool) -> dict:
    from verinoda.codecheck_ts import NODE_BUILTINS, Resolver

    decls: dict[str, list[dict]] = {}
    for it in items:
        decls.setdefault(it["name"], []).append(it)
    src = [f for f in files if f.endswith(_JS_SUFFIXES)]
    if not src and not decls:
        return {}
    own = set()
    for rel in [f for f in files if PurePosixPath(f).name == "package.json"]:
        try:
            name = json.loads(_read(repo / rel) or "{}").get("name")
        except (ValueError, AttributeError):
            name = None
        if isinstance(name, str):
            own.add(name.lower())
    r = Resolver(repo)
    uses: dict[str, list[Use]] = {}
    for rel in src:
        for spec, line, type_only in js_imports(_read(repo / rel) or "", rel):
            if spec.startswith((".", "/", "node:")) or ":" in spec or spec.split("/")[0] in NODE_BUILTINS:
                continue
            kind, _p, _why = r.resolve(spec, repo / rel)
            if kind in ("file", "missing", "ambient"):   # the project's own file, a tsconfig path, `declare module`
                continue
            parts = spec.split("/")
            pkg = ("/".join(parts[:2]) if spec.startswith("@") else parts[0]).lower()
            if not _NPM_NAME.match(pkg) or pkg in own:   # an alias the bundler resolves (~, @/, #, $)
                continue
            uses.setdefault(pkg, []).append(Use("npm", spec, rel, line, is_dev_file(rel), type_only))
    locks = [f for f in files if PurePosixPath(f).name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                                                            "npm-shrinkwrap.json")]
    lock_cache: dict = {}
    for pkg in sorted(uses):
        types = "@types/" + (pkg[1:].replace("/", "__") if pkg.startswith("@") else pkg)
        mine = [u for u in uses[pkg] if any(_in_scope(d, u.path) for d in decls.get(pkg, []) + decls.get(types, []))]
        rest = [u for u in uses[pkg] if u not in mine and not u.optional]
        if not rest:
            continue
        lock = _lock_line(repo, locks, pkg, lock_cache)
        nm = r._node_modules(repo / rest[0].path)
        installed = nm is not None and (nm / pkg / "package.json").is_file()
        if lock or installed:
            extra = [out.ev(lock[0], lock[1], "lock")] if lock else []
            where = f"{lock[0]}:{lock[1]}" if lock else f"{nm.relative_to(repo).as_posix()}/{pkg}"
            out.add("transitive_only", "npm", pkg, "statically_verified" if all_read else "strong_inference",
                    f"{pkg} is imported but not declared in the package.json that applies; it is installed "
                    f"({where}) only as another package's dependency", rest, extra=extra,
                    next_step=f"add {pkg} to dependencies (or devDependencies for test code)")
        else:
            out.add("missing", "npm", pkg, "strong_inference",
                    f"{pkg} is imported but no package.json declares it and no lock file or node_modules holds it",
                    rest, next_step=f"add {pkg} to package.json, or check that the name is an alias your bundler "
                                    "resolves")
    for pkg, ds in sorted(decls.items()):
        if pkg.startswith("@types/") or pkg in own:
            continue
        pkg_uses = [u for u in uses.get(pkg, []) if any(_in_scope(d, u.path) for d in ds)]
        _judge_declared(out, "npm", pkg, ds, pkg_uses, pkg)
    return {"files": len(src), "declared": len(decls), "imported_packages": len(uses)}


# -- Gradle and Maven ------------------------------------------------------------------------------------

def _jvm_match(imp: str, decls: dict[str, list[dict]]) -> list[str]:
    """The declared artifacts an import most likely comes from (the most specific match wins)."""
    best, got = 0, []
    segs = imp.split(".")
    for name in decls:
        group, _, art = name.partition(":")
        score = 0
        for pre in JVM_ALIASES.get(name, ()):
            if imp.startswith(pre.rstrip(".") + ".") or imp == pre.rstrip("."):
                score = max(score, 1000 + len(pre))
        g = group.replace("-", ".")
        if imp.startswith(g + "."):
            score = max(score, len(g))
        token = art.replace("-", "").replace("_", "")
        if token and token not in ("api", "core", "common", "lib", "main") and token in segs:
            score = max(score, 500)
        if score > best:
            best, got = score, [name]
        elif score and score == best:
            got.append(name)
    return got


def _jvm(out: _Out, repo: Path, files: list[str], items: list[dict]) -> dict:
    decls: dict[str, list[dict]] = {}
    for it in items:
        if it.get("ecosystem") != "maven" or it.get("build") == "other":
            continue
        scope = str(it.get("scope") or "")
        if not _JVM_COMPILE.match(scope) or it["name"].startswith("org.jetbrains.kotlin:"):
            continue   # bundled, run-time only or processors: no import is expected
        decls.setdefault(it["name"], []).append(it)
    src = [f for f in files if f.endswith((".java", ".kt"))]
    if not src or not decls:
        return {"files": len(src), "declared": len(decls)} if decls or src else {}
    texts = {rel: _read(repo / rel) or "" for rel in src}
    own = {m.group(1) for t in texts.values() for m in _JVM_PACKAGE.finditer(t)}
    uses: dict[str, list[Use]] = {}
    unmatched: set[str] = set()
    from verinoda.guards import code_text

    for rel, text in texts.items():
        code = code_text(text, PurePosixPath(rel).suffix, keep_strings=True)
        for m in _JVM_IMPORT.finditer(code):
            imp = m.group(1)
            pkg = imp.rpartition(".")[0]
            if imp.startswith(_JVM_PLATFORM) or any(imp.startswith(o + ".") or pkg == o for o in own):
                continue
            line = code.count("\n", 0, m.start(1)) + 1
            hits = [n for n in _jvm_match(imp, decls) if any(_in_scope(d, rel) for d in decls[n])]
            if not hits:
                unmatched.add(".".join(imp.split(".")[:3]))
            for n in hits:
                uses.setdefault(n, []).append(Use("maven", imp, rel, line, is_dev_file(rel)))
    for name, ds in sorted(decls.items()):
        _judge_declared(out, "maven", name, ds, uses.get(name, []), name)
    if unmatched:
        out.limits.append(f"jvm: {len(unmatched)} imported package(s) match no declared artifact and are not judged "
                          "(the JDK, the platform, a transitive jar or a name the matching misses; the classpath is "
                          f"not read): {', '.join(sorted(unmatched)[:5])}{' ...' if len(unmatched) > 5 else ''}")
    return {"files": len(src), "declared": len(decls), "imported_artifacts": len(uses)}


def check_deps(repo: Path, *, env: str | None = "auto", all_files: list[str] | None = None) -> dict:
    """Declared against used dependencies of the project (see the module docstring). ``exit``: 3 when
    something is found, 4 when no manifest was read (nothing checked), else 0."""
    from verinoda.guards import _excluded_roots, declared_dependencies
    from verinoda.snapshot import list_files

    repo = Path(repo).resolve()
    listed = all_files if all_files is not None else list_files(repo)
    deps = declared_dependencies(repo, listed)
    items = [dict(it) for it in deps.get("items") or []]
    if "pyproject.toml" in (deps.get("manifests") or []):
        items += _python_groups(repo)
    roots = _excluded_roots(repo)
    files = [f for f in listed if not set(PurePosixPath(f).parts[:-1]) & _NOT_PROJECT
             and not any(f.startswith(r + "/") for r in roots)]
    out = _Out(repo)
    all_read = not deps.get("unread") and not deps.get("error")
    if deps.get("unread"):
        un = deps["unread"]
        out.limits.append(f"{len(un)} other manifest(s) are not read, so a package they declare may be reported: "
                          f"{', '.join(un[:5])}{' ...' if len(un) > 5 else ''}")
    if deps.get("error"):
        out.limits.append(f"a manifest could not be read: {deps['error']}")
    eco: dict[str, dict] = {}
    got = _python(out, repo, files, [i for i in items if i.get("ecosystem") == "python"], env, all_read)
    if got:
        eco["python"] = got
    got = _npm(out, repo, files, [i for i in items if i.get("ecosystem") == "npm"], all_read)
    if got:
        eco["npm"] = got
    got = _jvm(out, repo, files, items)
    if got:
        eco["gradle_maven"] = got
    others = sorted({str(i.get("ecosystem")) for i in items} - {"python", "npm", "maven", "gradle-plugin"})
    if others:
        out.limits.append(f"declared {', '.join(others)} dependencies are listed by the manifests but not checked")
    out.limits.append("unused and wrong_group are strong_inference: a package can be used without an import, and "
                      "which files are dev code is a path rule")
    summary = {f: sum(1 for x in out.findings if x["finding"] == f) for f in FINDINGS}
    manifests = deps.get("manifests") or []
    code = 3 if out.findings else 4 if not manifests else 0
    why = (f"{len(out.findings)} finding(s)" if code == 3 else
           "no manifest or build file was read: nothing was checked" if code == 4 else "")
    return {"kind": "deps", "manifests": manifests, "ecosystems": eco, "summary": summary,
            "findings": out.findings, "limits": out.limits, "exit": code, **({"exit_because": why} if why else {})}


def render(r: dict) -> None:
    s = r["summary"]
    print(f"verinoda check --deps: {s['missing']} missing, {s['transitive_only']} transitive only, "
          f"{s['unused']} unused, {s['wrong_group']} in the wrong group")
    print(f"manifests: {', '.join(r['manifests']) or 'none found'}")
    for name, e in r["ecosystems"].items():
        print(f"{name}: {e.get('declared', 0)} declared, {e.get('files', 0)} files read"
              + (f", environment {e['environment']}" if e.get("environment") else ""))
    if r.get("exit_because"):
        print(f"exit {r['exit']}: {r['exit_because']}")
    for f in r["findings"]:
        print(f"{f['finding'].upper():<16} {f['package']}  ({f['ecosystem']}, {f['status']})")
        print(f"    {f['claim']}")
        for ev in f["evidence"]:
            print(f"    {ev['role']:<11} {ev['locator']}  {ev['excerpt']}")
        more = f.get("import_sites", 0) - EVIDENCE_SITES
        if more > 0:
            print(f"    ... {more} more import site(s)")
        if f.get("next_step"):
            print(f"    next: {f['next_step']}")
    for lim in r["limits"]:
        print(f"limit: {lim}")
