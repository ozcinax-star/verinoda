"""The workspace packages of a monorepo and the ones a change affects: ``review``'s ``affected`` key and
``verinoda affected``.

Packages come from the manifests, read as text (nothing is run or installed):

- **npm, yarn, pnpm**: the root ``package.json`` ``workspaces`` globs and ``pnpm-workspace.yaml`` ``packages``,
  matched as the package managers match them (:func:`verinoda.guards.ws_match`); each package's ``name``; its
  ``dependencies``, ``devDependencies``, ``peerDependencies`` and ``optionalDependencies``; its ``scripts`` are
  its build targets;
- **Cargo**: the root ``[workspace]`` ``members`` (globs; ``exclude`` honoured), the root itself when it has a
  ``[package]``; every ``*dependencies`` table (``[target.*.dependencies]`` too), ``package = "..."`` renames;
- **Gradle**: ``include`` in the root ``settings.gradle(.kts)``; ``project(":a")`` and the type-safe
  ``projects.a`` accessors in each project's build file (comments blanked);
- **Maven**: the root pom's ``<modules>``, recursively; each module's ``artifactId`` (its ``<parent>`` block left
  out) and the ``<dependency>`` entries outside ``<dependencyManagement>``;
- **Python**: every ``pyproject.toml`` with a ``[project]`` or ``[tool.poetry]`` name when one of them is in a
  subfolder; PEP 508 names in ``dependencies``, optional and PEP 735 groups, Poetry dependency tables;
- **Go**: every ``go.mod`` when there are several (or a ``go.work``); ``require`` and ``replace`` lines.

Python and Go declare no workspace list, so their manifests under test folders and the folders the guards take
for samples, fixtures and vendored code (``examples/``, ``fixtures/``, ``vendor/`` ...) are left out. A changed
file belongs to the innermost package folder that holds it; the affected packages are those changed and, walking the declared dependencies backwards, every package that depends on one of them, each
with the manifest line that declares the dependency. A dependency on a workspace package's name is
``statically_verified`` when the manifest says it is the local one (``workspace:`` or ``file:`` specs, a Cargo
``path`` or ``workspace = true``, a Gradle ``project(...)``, a Go ``replace`` or ``go.work``, a Poetry ``path``
or uv ``workspace = true`` source, a Maven module of the same group); otherwise the package manager may take the
published one, and it is ``strong_inference``. "Affected" means "declares a dependency on a changed package", as
``nx affected`` reads it, not that the changed code is used.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Callable

from verinoda import evidence as evmod

MAX_LISTED = 40        # affected rows listed (the rest counted)
MAX_OUTSIDE = 10       # changed files outside every package listed
MAX_TARGETS = 8        # npm scripts listed per package
MAX_DEPTH = 20         # dependency levels walked back
_SKIP_PARTS = {"node_modules", "bower_components", ".git", ".verinoda", "target", "build", "dist", ".venv", "venv"}
# folders whose own pyproject.toml or go.mod is a test, sample or fixture, not a workspace package (no workspace
# list names them): the guards' sample and vendored folders, and test folders
_UNDECLARED_SKIP = {"test", "tests", "testdata", "tests_upstream"}
LIMITS = [
    "affected = declares a dependency on a changed package (as nx affected reads it), not that the changed code "
    "is used",
    "a change outside every package (a root build file, a lock file, shared configuration) may affect them all: "
    "listed under outside, not followed",
    "Gradle projectDir overrides, Maven profiles and version-catalog bundles of projects are not read",
    "build targets: npm scripts only",
]


def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _norm_cargo(name: str) -> str:
    return name.replace("_", "-").lower()


def _blank_xml_comments(text: str) -> str:
    return re.sub(r"<!--.*?(?:-->|\Z)", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _pkg(name: str, d: str, eco: str, manifest: str, line: int, text: str) -> dict:
    return {"name": name, "dir": d, "ecosystem": eco, "manifest": manifest, "at": f"{manifest}:{line}",
            "line_text": text.strip()[:200], "deps": [], "targets": []}


def _dep(pkg: dict, raw: str, line: int, text: str, local: bool) -> None:
    pkg["deps"].append({"raw": raw, "at": f"{pkg['manifest']}:{line}", "line_text": text.strip()[:200],
                        "local": local})


def _skipped(rel: str, extra: set[str] = frozenset()) -> bool:
    parts = set(PurePosixPath(rel).parent.parts)
    return bool(parts & _SKIP_PARTS or parts & extra)


def _undeclared_skipped(rel: str) -> bool:
    from verinoda.guards import _NOT_PRODUCT_DIRS

    return _skipped(rel, _UNDECLARED_SKIP | _NOT_PRODUCT_DIRS)


# -- npm / yarn / pnpm ---------------------------------------------------------------------------------------
def _npm(files: set[str], read: Callable[[str], str | None]) -> list[dict]:
    from verinoda.guards import ws_match

    globs: list[tuple[str, str]] = []   # (glob, where declared)
    root = read("package.json") if "package.json" in files else None
    if root:
        try:
            data = json.loads(root)
        except ValueError:
            data = None
        ws = data.get("workspaces") if isinstance(data, dict) else None
        if isinstance(ws, dict):
            ws = ws.get("packages")
        lines = root.split("\n")
        for g in ws or []:
            if isinstance(g, str):
                ln = next((i for i, t in enumerate(lines, 1) if f'"{g}"' in t), 1)
                globs.append((g, f"package.json:{ln}"))
    if "pnpm-workspace.yaml" in files:
        block = False
        for i, ln in enumerate((read("pnpm-workspace.yaml") or "").split("\n"), 1):
            s = ln.split("#")[0].rstrip()
            if re.match(r"^packages\s*:", s):
                inline = re.search(r"\[(.*)\]", s)
                block = inline is None
                for x in (inline.group(1).split(",") if inline else []):
                    if x.strip():
                        globs.append((x.strip().strip("'\""), f"pnpm-workspace.yaml:{i}"))
                continue
            if block and re.match(r"^\S", s):
                block = False
            m = re.match(r"^\s*-\s*['\"]?([^'\"]+?)['\"]?\s*$", s) if block else None
            if m:
                globs.append((m.group(1), f"pnpm-workspace.yaml:{i}"))
    pos = [(g.strip(), w) for g, w in globs if g.strip() and not g.strip().startswith("!")]
    neg = [g.strip()[1:] for g, _ in globs if g.strip().startswith("!")]
    if not pos:
        return []
    out: list[dict] = []
    for rel in sorted(f for f in files if PurePosixPath(f).name == "package.json" and f != "package.json"):
        d = PurePosixPath(rel).parent.as_posix()
        hit = next((w for g, w in pos if ws_match(d, g)), None)
        if hit is None or _skipped(rel) or any(ws_match(d, g) for g in neg):
            continue
        raw = read(rel)
        try:
            pj = json.loads(raw or "")
        except ValueError:
            continue
        if not isinstance(pj, dict):
            continue
        lines = (raw or "").split("\n")
        name = pj.get("name") if isinstance(pj.get("name"), str) else d
        ln = next((i for i, t in enumerate(lines, 1) if re.search(r'"name"\s*:', t)), 1)
        p = _pkg(name, d, "npm", rel, ln, lines[ln - 1] if lines else "")
        p["declared_at"] = hit
        for sect in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            deps = pj.get(sect)
            if not isinstance(deps, dict):
                continue
            for dn, spec in deps.items():
                i = next((i for i, t in enumerate(lines, 1) if re.search(rf'"{re.escape(str(dn))}"\s*:', t)), 1)
                local = isinstance(spec, str) and spec.startswith(("workspace:", "file:", "link:", "portal:"))
                _dep(p, str(dn), i, lines[i - 1], local)
        scripts = pj.get("scripts")
        if isinstance(scripts, dict):
            p["targets"] = [str(k) for k in scripts][:MAX_TARGETS]
        out.append(p)
    return out


# -- Cargo ---------------------------------------------------------------------------------------------------
def _toml_strings(lines: list[str], start: int) -> list[tuple[str, int]]:
    """The quoted strings of the array that starts on line index ``start`` (``key = [`` ... ``]``)."""
    out: list[tuple[str, int]] = []
    seg = lines[start].split("=", 1)[1] if "=" in lines[start] else lines[start]
    i = start
    while True:
        code = seg.split("#")[0]
        out += [(m.group(1), i + 1) for m in re.finditer(r"['\"]([^'\"]*)['\"]", code)]
        if "]" in code or i + 1 >= len(lines):
            return out
        i += 1
        seg = lines[i]


def _sections(text: str):
    """(line number, section name or "", line) for each line of a TOML file."""
    sect = ""
    for i, ln in enumerate(text.split("\n"), 1):
        m = re.match(r"\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$", ln)
        if m:
            sect = m.group(1).replace('"', "").replace("'", "")
        yield i, sect, ln


def _cargo_member(rel: str, text: str, declared: str | None) -> dict | None:
    name = None
    for i, sect, ln in _sections(text):
        m = re.match(r"\s*name\s*=\s*['\"]([^'\"]+)['\"]", ln)
        if sect == "package" and m:
            name = (m.group(1), i, ln)
            break
    if name is None:
        return None
    d = PurePosixPath(rel).parent.as_posix()
    p = _pkg(name[0], "" if d == "." else d, "cargo", rel, name[1], name[2])
    if declared:
        p["declared_at"] = declared
    for i, sect, ln in _sections(text):
        code = ln.split("#")[0]
        head = re.match(r"\s*\[\s*(.*?dependencies)\.([\w\-]+)\s*\]", code)
        if head and not head.group(1).startswith("workspace"):   # [dependencies.foo]
            _dep(p, head.group(2), i, ln, False)
            continue
        if not sect.endswith("dependencies") or sect.startswith("workspace") or "." in sect.split("dependencies")[-1]:
            continue
        m = re.match(r"\s*([\w\-]+)\s*(?:\.\s*workspace\s*)?=", code)
        if not m:
            continue
        ren = re.search(r"\bpackage\s*=\s*['\"]([^'\"]+)['\"]", code)
        local = bool(re.search(r"\bpath\s*=|\bworkspace\s*=\s*true|\.\s*workspace\s*=\s*true", code))
        _dep(p, ren.group(1) if ren else m.group(1), i, ln, local)
    # [dependencies.foo] tables: their path / workspace keys follow the header
    cur = None
    for i, sect, ln in _sections(text):
        if re.match(r"\s*\[", ln):
            cur = next((dp for dp in p["deps"] if dp["at"] == f"{rel}:{i}"), None)
            continue
        if cur is not None:
            code = ln.split("#")[0]
            if re.search(r"^\s*(path|workspace)\s*=", code):
                cur["local"] = True
            ren = re.match(r"\s*package\s*=\s*['\"]([^'\"]+)['\"]", code)
            if ren:
                cur["raw"] = ren.group(1)
    return p


def _cargo(files: set[str], read: Callable[[str], str | None]) -> list[dict]:
    from verinoda.guards import ws_match

    root = read("Cargo.toml") if "Cargo.toml" in files else None
    if not root:
        return []
    lines = root.split("\n")
    members: list[tuple[str, int]] = []
    exclude: list[str] = []
    for i, sect, ln in _sections(root):
        if sect == "workspace" and re.match(r"\s*members\s*=", ln):
            members = _toml_strings(lines, i - 1)
        if sect == "workspace" and re.match(r"\s*exclude\s*=", ln):
            exclude = [s for s, _ in _toml_strings(lines, i - 1)]
    if not members:
        return []
    out: list[dict] = []
    own = _cargo_member("Cargo.toml", root, None)
    if own is not None:
        out.append(own)
    for rel in sorted(f for f in files if PurePosixPath(f).name == "Cargo.toml" and f != "Cargo.toml"):
        d = PurePosixPath(rel).parent.as_posix()
        hit = next((ln for g, ln in members if ws_match(d, g)), None)
        if hit is None or _skipped(rel) or any(ws_match(d, g) for g in exclude):
            continue
        p = _cargo_member(rel, read(rel) or "", f"Cargo.toml:{hit}")
        if p is not None:
            out.append(p)
    return out


# -- Gradle --------------------------------------------------------------------------------------------------
def _accessor(path: str) -> str:
    """The type-safe accessor of a Gradle project path (``:foo-bar:baz`` -> ``fooBar.baz``)."""
    def camel(s: str) -> str:
        parts = re.split(r"[-_]", s)
        return parts[0] + "".join(x[:1].upper() + x[1:] for x in parts[1:])
    return ".".join(camel(s) for s in path.strip(":").split(":"))


def _gradle(files: set[str], read: Callable[[str], str | None]) -> list[dict]:
    from verinoda.guards import build_code

    settings = next((s for s in ("settings.gradle.kts", "settings.gradle") if s in files), None)
    if settings is None:
        return []
    text = build_code(read(settings) or "", settings)
    raw_lines = (read(settings) or "").split("\n")
    out: list[dict] = []
    dirs = {str(PurePosixPath(f).parent) for f in files}
    for i, ln in enumerate(text.split("\n"), 1):
        if not re.match(r"\s*include\b", ln):
            continue
        for proj in re.findall(r"['\"](:?[\w.\-:]+)['\"]", ln):
            path = ":" + proj.strip(":")
            d = path.strip(":").replace(":", "/")
            if d not in dirs and not any(x.startswith(d + "/") for x in dirs):
                continue
            build = next((f"{d}/{b}" for b in ("build.gradle.kts", "build.gradle") if f"{d}/{b}" in files), None)
            p = _pkg(path, d, "gradle", build or settings, 1 if build else i,
                     (read(build) or "").split("\n")[0] if build else raw_lines[i - 1])
            p["at"] = f"{settings}:{i}"
            p["line_text"] = raw_lines[i - 1].strip()[:200]
            p["declared_at"] = f"{settings}:{i}"
            if build:
                btext = read(build) or ""
                blines = btext.split("\n")
                for j, code in enumerate(build_code(btext, build).split("\n"), 1):
                    for m in re.finditer(r"project\s*\(\s*(?:path\s*[:=]\s*)?['\"](:[\w.\-:]+)['\"]", code):
                        _dep(p, m.group(1), j, blines[j - 1], True)
                    for m in re.finditer(r"\bprojects\.([\w.]+)", code):
                        _dep(p, "accessor:" + m.group(1), j, blines[j - 1], False)
            out.append(p)
    acc = {_accessor(p["name"]): p["name"] for p in out}
    for p in out:
        for dp in p["deps"]:
            if dp["raw"].startswith("accessor:"):
                a = dp["raw"][len("accessor:"):]
                dp["raw"] = acc.get(a) or next((n for k, n in acc.items() if a.startswith(k + ".")), dp["raw"])
    return out


# -- Maven ---------------------------------------------------------------------------------------------------
def _xml_block(text: str, tag: str) -> str:
    """``text`` with every ``<tag>...</tag>`` block blanked (line breaks kept)."""
    return re.sub(rf"<{tag}\b.*?</{tag}>", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def _maven(files: set[str], read: Callable[[str], str | None]) -> list[dict]:
    if "pom.xml" not in files:
        return []
    out: list[dict] = []
    todo, seen = ["pom.xml"], {"pom.xml"}
    seen_at: dict[str, str] = {}
    root_group = None
    while todo:
        pom = todo.pop(0)
        raw = read(pom) or ""
        text = _blank_xml_comments(raw)
        lines = raw.split("\n")
        base = pom.rpartition("/")[0]
        own = _xml_block(_xml_block(_xml_block(_xml_block(text, "parent"), "dependencies"), "build"), "profiles")
        own = _xml_block(_xml_block(own, "dependencyManagement"), "modules")
        parent = re.search(r"<parent\b.*?</parent>", text, re.S)
        pgroup = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", parent.group(0)) if parent else None
        group = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", own)
        group_id = group.group(1) if group else (pgroup.group(1) if pgroup else root_group)
        if pom == "pom.xml":
            root_group = group_id
        if pom != "pom.xml":
            art = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", own)
            if art:
                ln = _line_of(text, art.start())
                p = _pkg(art.group(1), base, "maven", pom, ln, lines[ln - 1])
                p["group"] = group_id
                p["declared_at"] = seen_at.get(pom)
                deps_text = _xml_block(_xml_block(text, "dependencyManagement"), "parent")
                for m in re.finditer(r"<dependency>(.*?)</dependency>", deps_text, re.S):
                    a = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", m.group(1))
                    g = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", m.group(1))
                    if a:
                        j = _line_of(deps_text, m.start(1) + a.start())
                        gid = g.group(1) if g else None
                        if gid in ("${project.groupId}", "${project.parent.groupId}"):
                            gid = group_id
                        _dep(p, a.group(1), j, lines[j - 1], False)
                        p["deps"][-1]["group"] = gid
                out.append(p)
        mods = re.search(r"<modules>(.*?)</modules>", text, re.S)
        for m in re.finditer(r"<module>\s*([^<]+?)\s*</module>", mods.group(0) if mods else ""):
            sub = PurePosixPath(base, m.group(1).strip().strip("/"), "pom.xml").as_posix()
            if sub in files and sub not in seen and ".." not in sub.split("/"):
                seen.add(sub)
                seen_at[sub] = f"{pom}:{_line_of(text, mods.start() + m.start())}"
                todo.append(sub)
    by_art = {p["name"]: p for p in out}
    for p in out:
        for dp in p["deps"]:
            tgt = by_art.get(dp["raw"])
            dp["local"] = tgt is not None and dp.pop("group", None) in (tgt.get("group"), None)
            dp.pop("group", None)
    return out


# -- Python --------------------------------------------------------------------------------------------------
def _pep508(spec: str) -> str | None:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return m.group(1) if m else None


def _python(files: set[str], read: Callable[[str], str | None]) -> list[dict]:
    rels = sorted(f for f in files if PurePosixPath(f).name == "pyproject.toml"
                  and not _undeclared_skipped(f))
    if not any("/" in r for r in rels):
        return []
    out: list[dict] = []
    for rel in rels:
        text = read(rel) or ""
        lines = text.split("\n")
        name = None
        for i, sect, ln in _sections(text):
            m = re.match(r"\s*name\s*=\s*['\"]([^'\"]+)['\"]", ln)
            if m and sect in ("project", "tool.poetry"):
                name = (m.group(1), i, ln)
                break
        if name is None:
            continue
        d = PurePosixPath(rel).parent.as_posix()
        p = _pkg(name[0], "" if d == "." else d, "python", rel, name[1], name[2])
        local: set[str] = set()
        arr_until = -1
        for i, sect, ln in _sections(text):
            code = ln.split("#")[0]
            if i <= arr_until:
                continue
            in_arrays = ((sect == "project" and re.match(r"\s*dependencies\s*=", code))
                         or (sect in ("project.optional-dependencies", "dependency-groups")
                             and re.match(r"\s*[\w\-.\"']+\s*=\s*\[", code)))
            if in_arrays:
                strs = _toml_strings(lines, i - 1)
                for s, j in strs:
                    n = _pep508(s)
                    if n:
                        _dep(p, n, j, lines[j - 1], False)
                arr_until = strs[-1][1] if strs else i
                continue
            poetry = (sect in ("tool.poetry.dependencies", "tool.poetry.dev-dependencies")
                      or re.fullmatch(r"tool\.poetry\.group\.[\w\-]+\.dependencies", sect))
            m = re.match(r"\s*['\"]?([A-Za-z0-9][\w.\-]*)['\"]?\s*=", code)
            if poetry and m and m.group(1) != "python":
                _dep(p, m.group(1), i, ln, bool(re.search(r"\bpath\s*=", code)))
            if sect == "tool.uv.sources" and m and re.search(r"\bworkspace\s*=\s*true|\bpath\s*=", code):
                local.add(_norm_py(m.group(1)))
        for dp in p["deps"]:
            dp["local"] = dp["local"] or _norm_py(dp["raw"]) in local
        out.append(p)
    return out if len(out) > 1 else []


# -- Go ------------------------------------------------------------------------------------------------------
def _go(files: set[str], read: Callable[[str], str | None]) -> list[dict]:
    rels = sorted(f for f in files if PurePosixPath(f).name == "go.mod" and not _undeclared_skipped(f))
    used: dict[str, str] = {}
    if "go.work" in files:
        for i, ln in enumerate((read("go.work") or "").split("\n"), 1):
            for m in re.finditer(r"(?:^|\s)(\.{1,2}/[\w./\-]*|\.)(?=\s|$|\))", ln.split("//")[0]):
                d = PurePosixPath(m.group(1)).as_posix()
                if not d.startswith(".."):
                    used["" if d == "." else d] = f"go.work:{i}"
    if len(rels) < 2 and not used:
        return []
    out: list[dict] = []
    for rel in rels:
        text = read(rel) or ""
        lines = text.split("\n")
        mod = next(((m.group(1), i) for i, ln in enumerate(lines, 1)
                    for m in [re.match(r"\s*module\s+(\S+)", ln)] if m), None)
        if mod is None:
            continue
        d = PurePosixPath(rel).parent.as_posix()
        d = "" if d == "." else d
        p = _pkg(mod[0], d, "go", rel, mod[1], lines[mod[1] - 1])
        if d in used:
            p["declared_at"] = used[d]
        block = None
        for i, ln in enumerate(lines, 1):
            code = ln.split("//")[0]
            m = re.match(r"\s*(require|replace)\s*\(\s*$", code)
            if m:
                block = m.group(1)
                continue
            if block and code.strip() == ")":
                block = None
                continue
            m = re.match(r"\s*(?:(require|replace)\s+)?(\S+)(.*)$", code)
            kind = (m.group(1) or block) if m else None
            if not m or kind is None or not code.strip():
                continue
            if kind == "require":
                _dep(p, m.group(2), i, ln, False)
            elif "=>" in m.group(3) and re.search(r"=>\s*\.{1,2}/", m.group(3)):
                for dp in p["deps"]:
                    if dp["raw"] == m.group(2):
                        dp["local"] = True
                p.setdefault("_replaced", set()).add(m.group(2))
        out.append(p)
    dir_of = {p["name"]: p["dir"] for p in out}
    for p in out:
        rep = p.pop("_replaced", set())
        for dp in p["deps"]:   # go.work using both modules, or a replace to a local folder: the local module
            dp["local"] = dp["local"] or dp["raw"] in rep or (p["dir"] in used and dir_of.get(dp["raw"]) in used)
    return out


# -- the workspace ---------------------------------------------------------------------------------------------
def workspace(files: list[str], read: Callable[[str], str | None]) -> dict:
    """Every workspace package the manifests declare, with its internal dependencies resolved to package names."""
    fs = set(files)
    pkgs: list[dict] = []
    for find in (_npm, _cargo, _gradle, _maven, _python, _go):
        pkgs += find(fs, read)
    keys: dict[tuple[str, str], dict] = {}
    for p in pkgs:
        norm = {"python": _norm_py, "cargo": _norm_cargo}.get(p["ecosystem"], lambda s: s)
        keys.setdefault((p["ecosystem"], norm(p["name"])), p)
    for p in pkgs:
        norm = {"python": _norm_py, "cargo": _norm_cargo}.get(p["ecosystem"], lambda s: s)
        internal = []
        for dp in p["deps"]:
            tgt = keys.get((p["ecosystem"], norm(dp["raw"])))
            if tgt is not None and tgt is not p:
                internal.append({**dp, "on": tgt["name"], "on_dir": tgt["dir"]})
        p["deps"] = internal
    return {"packages": pkgs}


def _owner(rel: str, pkgs: list[dict]) -> dict | None:
    best = None
    for p in pkgs:
        d = p["dir"]
        if (d == "" or rel == d or rel.startswith(d + "/")) and (best is None or len(d) > len(best["dir"])):
            best = p
    return best


def _ev(at: str, text: str) -> dict:
    return {"source_type": "source_code", "locator": at, "excerpt": text,
            "content_hash": evmod.content_hash(text), "meta": {"file": at.rsplit(":", 1)[0]}}


def affected(files: list[str], read: Callable[[str], str | None], changed: list[str]) -> dict:
    """The ``affected`` block: the workspace packages ``changed`` (repository-relative paths) falls in, then every
    package that declares a dependency on one of them, nearest first."""
    ws = workspace(files, read)
    pkgs = ws["packages"]
    out: dict = {"packages_total": len(pkgs), "affected": [], "outside": [], "method": (
        "packages from the workspace manifests (npm/yarn/pnpm workspaces, Cargo members, Gradle include, Maven "
        "modules, Python and Go modules in subfolders); a changed file belongs to the innermost package folder; "
        "dependents by the packages' declared dependencies, walked back"), "limits": list(LIMITS)}
    if len(pkgs) < 2:
        out["not_checked"] = ["not a monorepo: fewer than two workspace packages declared"]
        out.pop("affected")
        out.pop("outside")
        return out
    eco: dict[str, int] = {}
    for p in pkgs:
        eco[p["ecosystem"]] = eco.get(p["ecosystem"], 0) + 1
    out["ecosystems"] = eco
    hit: dict[int, list[str]] = {}
    outside: list[str] = []
    for rel in sorted(set(changed)):
        p = _owner(rel, pkgs)
        if p is None:
            outside.append(rel)
        else:
            hit.setdefault(id(p), []).append(rel)
    rows: list[dict] = []
    status_of: dict[int, str] = {}
    for p in pkgs:
        if id(p) not in hit:
            continue
        fs = hit[id(p)]
        text = (f"{p['name']} ({p['ecosystem']} package at {p['dir'] or '.'}/, declared at {p['at']}) holds "
                f"{len(fs)} changed file(s): {', '.join(fs[:3])}{' ...' if len(fs) > 3 else ''}")
        rows.append({"name": p["name"], "dir": p["dir"], "ecosystem": p["ecosystem"], "reason": "changed",
                     "files": len(fs), "at": p["at"], "targets": p["targets"],
                     "claim": {"kind": "structure", "status": "statically_verified", "text": text,
                               "evidence": [_ev(p["at"], p["line_text"])]}})
        status_of[id(p)] = "statically_verified"
    # walk the declared dependencies back from the changed packages
    by_name = {(p["ecosystem"], p["name"]): p for p in pkgs}
    frontier = [p for p in pkgs if id(p) in hit]
    chains: dict[int, list[dict]] = {id(p): [] for p in frontier}
    depth = 0
    while frontier and depth < MAX_DEPTH:
        depth += 1
        nxt = []
        for q in pkgs:
            if id(q) in chains:
                continue
            for dp in q["deps"]:
                tgt = by_name.get((q["ecosystem"], dp["on"]))
                if tgt is None or id(tgt) not in {id(f) for f in frontier}:
                    continue
                chain = [{**dp, "from": q["name"]}, *chains[id(tgt)]]
                chains[id(q)] = chain
                weak = any(not h["local"] for h in chain)
                st = "strong_inference" if weak or status_of.get(id(tgt)) == "strong_inference" \
                    else "statically_verified"
                status_of[id(q)] = st
                via = [q["name"], *[h["on"] for h in chain]]
                text = (f"{q['name']} declares a dependency on {dp['on']} ({dp['at']})"
                        + (f", which leads to the changed package {via[-1]} ({' -> '.join(via)})"
                           if len(chain) > 1 else ", a changed package"))
                unc = [] if st == "statically_verified" else [
                    "the dependency is declared by name: the package manager may take the published package "
                    "instead of the workspace one"]
                rows.append({"name": q["name"], "dir": q["dir"], "ecosystem": q["ecosystem"], "reason": "depends",
                             "via": via, "distance": len(chain), "at": dp["at"], "targets": q["targets"],
                             "claim": {"kind": "structure", "status": st, "text": text,
                                       "evidence": [_ev(h["at"], h["line_text"]) for h in chain[:4]],
                                       "uncertainties": unc}})
                nxt.append(q)
                break
        frontier = nxt
    out["affected_total"] = len(rows)
    out["affected"] = rows[:MAX_LISTED]
    out["outside"] = outside[:MAX_OUTSIDE]
    if len(outside) > MAX_OUTSIDE:
        out["outside_total"] = len(outside)
    for r in out["affected"]:
        if not r["targets"]:
            del r["targets"]
    return out


def compact(block: dict) -> dict:
    """The block without claim texts and evidence (the MCP response is capped): which packages, why, where."""
    if not block or "affected" not in block:   # not a monorepo: the count says so
        return {k: block[k] for k in ("packages_total",) if k in (block or {})}
    out = {"packages_total": block["packages_total"], "affected_total": block.get("affected_total", 0)}
    out["affected"] = [{"name": r["name"], "reason": r["reason"], "at": r["at"],
                        **({"via": r["via"]} if r.get("via") else {}), "status": r["claim"]["status"]}
                       for r in block["affected"][:15]]
    if block.get("outside"):
        out["outside"] = len(block.get("outside") or []) if "outside_total" not in block else block["outside_total"]
    return out


def summary_part(block: dict) -> str:
    rows = (block or {}).get("affected") or []
    if not rows:
        return ""
    ch = [r["name"] for r in rows if r["reason"] == "changed"]
    dep = [r["name"] for r in rows if r["reason"] == "depends"]
    return (f"Workspace packages affected: {block.get('affected_total', len(rows))} of {block['packages_total']} "
            f"({len(ch)} changed, {len(dep)} depending on them).")


def render(block: dict) -> list[str]:
    """Text lines for ``verinoda review`` and ``verinoda affected``."""
    if not block or "affected" not in block:
        return []
    out = ["", f"Affected workspace packages ({block.get('affected_total', 0)} of {block['packages_total']}: "
               "changed, then the packages that declare a dependency on them):"]
    for r in block["affected"]:
        how = (f"{r['files']} changed file(s)" if r["reason"] == "changed"
               else "via " + " -> ".join(r["via"]))
        tg = f"  targets: {', '.join(r['targets'])}" if r.get("targets") else ""
        out.append(f"  [{r['claim']['status']}] {r['name']} ({r['ecosystem']}, {r['dir'] or '.'}/): {how}  "
                   f"{r['at']}{tg}")
    if block.get("affected_total", 0) > len(block["affected"]):
        out.append(f"  ... {block['affected_total'] - len(block['affected'])} more (--json)")
    if block.get("outside"):
        n = block.get("outside_total", len(block["outside"]))
        out.append(f"  outside every package ({n}, not followed): {', '.join(block['outside'][:5])}"
                   + (" ..." if n > 5 else ""))
    if len(out) == 2:
        out.append("  none: no changed file is in a workspace package")
    return out


def run(repo, *, base: str | None = None, staged: bool = False, changed: list[str] | None = None) -> dict:
    """``verinoda affected``: the block for the working tree against ``base`` (default HEAD), the staged changes,
    or the files ``changed`` names (repository-relative), read from the tree the review would read."""
    from pathlib import Path

    from verinoda import review as rv
    from verinoda import treestate
    from verinoda.snapshot import list_files

    repo = Path(repo).resolve()
    texts: dict[str, str | None] = {}
    base_info = None
    if changed:
        files = list_files(repo)
        touched = sorted({re.sub(r"^(\./)+", "", c.replace("\\", "/")) for c in changed})
    else:
        sha = treestate.resolve_commit(repo, base or "HEAD")
        base_info = {"ref": treestate.check_ref(base or "HEAD"), "commit": sha}
        diffs, skipped = (rv._diff_staged if staged else rv._diff_worktree)(repo, sha)
        texts = {fd.rel: fd.new for fd in diffs}
        if staged:
            files, over = rv._staged_tree(repo)
            texts = {**over, **texts}
        else:
            files = list_files(repo)
        touched = [fd.rel for fd in diffs] + [s["file"] for s in skipped]

    def read(rel: str) -> str | None:
        if rel not in texts:
            try:
                texts[rel] = rv._decode((repo / rel).read_bytes())
            except OSError:
                texts[rel] = None
        return texts[rel]

    return {"base": base_info, "mode": "files" if changed else ("staged" if staged else "worktree"),
            "changed_files": len(touched), **affected(files, read, touched)}


def render_run(res: dict) -> str:
    """Text for ``verinoda affected``."""
    where = ("the files given" if res["mode"] == "files" else
             f"the {'staged changes' if res['mode'] == 'staged' else 'working tree'} against "
             f"{res['base']['ref']} ({res['base']['commit'][:10]})")
    out = [f"Workspace packages for {where}: {res['changed_files']} changed file(s), "
           f"{res['packages_total']} package(s) declared."]
    for why in res.get("not_checked") or []:
        out.append(f"  not checked: {why}")
    out += render(res)[1:] if "affected" in res else []
    return "\n".join(out)
