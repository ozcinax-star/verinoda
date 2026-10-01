"""Shaderpack lint and the ``#include`` graph of GLSL files.

An Iris or OptiFine shaderpack is a tree of stage programs (``shaders/gbuffers_terrain.fsh``, ``composite1.vsh``)
that pull shared code from ``#include "/lib/..."`` (a path from the pack's ``shaders/`` directory) or a path
relative to the including file; Minecraft core shaders do the same with ``#moj_import <name.glsl>``. A stage only
fails when the game compiles it, so a typo in an included file, a brace left open or an include that points nowhere
shows up as a pink world, not as a line.

:func:`includes` resolves every include into an edge (from file and line to the file it names, ``missing`` when
nothing is there, ``external`` for a vanilla ``#moj_import``); :func:`cycles` lists include cycles; :func:`lint`
reads each file and each stage expanded with its includes: brackets that do not balance (the first branch of every
``#if`` only, so both halves of an ``#ifdef``/``#else`` pair are not counted together), ``#if`` without
``#endif``, ``#version`` not first or written twice or inside an included file, a standard Iris/OptiFine uniform
used but never declared, a macro tested but never defined (the standard Iris/OptiFine macros and every ``#define``
or ``//#define`` option toggle of the pack count as defined). Read from the text, no compiler: what the syntax states is
``verified``; an undeclared uniform or an undefined macro is ``strong_inference`` (a loader or a properties file can
define it). ``glslangValidator``, when installed and asked for (``--glslang``), compiles each stage expanded with its
includes and its errors are mapped back to the file and line they came from.
"""
from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
from pathlib import Path

from verinoda.shaders import SHADER_SUFFIXES, _files, _strip_comments

_INC = re.compile(r"^\s*#\s*(include|moj_import)\s*[<\"]([^>\"]+)[>\"]")
_DIRECTIVE = re.compile(r"^\s*#\s*(\w+)\s*(.*)$")
_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
_OPTION = re.compile(r"^\s*//\s*#\s*define\s+([A-Za-z_]\w*)", re.M)
_PAIRS = {")": "(", "]": "[", "}": "{"}

# Iris / OptiFine stage programs; a file whose stem matches, under a ``shaders/`` directory, is a pack stage.
_STAGE = re.compile(r"^(?:gbuffers_\w+|shadow(?:comp\d*|_\w+)?|shadowcomp\d*|composite\d*(?:_\w)?|deferred\d*(?:_\w)?|"
                    r"final|prepare\d*(?:_\w)?|begin\d*(?:_\w)?|setup\d*(?:_\w)?|dh_\w+)$")

# The macros Iris and OptiFine define for every program (names, then prefixes of families).
STANDARD_MACROS = frozenset((
    "MC_VERSION", "MC_GL_VERSION", "MC_GLSL_VERSION", "IS_IRIS", "MC_RENDER_QUALITY", "MC_SHADOW_QUALITY",
    "MC_HAND_DEPTH", "MC_OLD_HAND_LIGHT", "MC_OLD_LIGHTING", "MC_ANISOTROPIC_FILTERING", "MC_NORMAL_MAP",
    "MC_SPECULAR_MAP", "MC_FXAA_LEVEL", "DISTANT_HORIZONS", "VOXY", "__VERSION__", "__LINE__", "__FILE__", "GL_ES",
))
STANDARD_MACRO_PREFIXES = ("MC_OS_", "MC_GL_VENDOR_", "MC_GL_RENDERER_", "MC_TEXTURE_FORMAT_", "MC_GL_",
                           "IRIS_", "DH_BLOCK_", "GL_")

# Uniforms Iris and OptiFine fill; a program must still declare each one it uses.
STANDARD_UNIFORMS = frozenset((
    "gbufferModelView", "gbufferModelViewInverse", "gbufferProjection", "gbufferProjectionInverse",
    "gbufferPreviousModelView", "gbufferPreviousProjection", "shadowModelView", "shadowModelViewInverse",
    "shadowProjection", "shadowProjectionInverse", "cameraPosition", "previousCameraPosition",
    "cameraPositionInt", "cameraPositionFract", "sunPosition", "moonPosition", "shadowLightPosition",
    "upPosition", "sunAngle", "shadowAngle", "frameTimeCounter", "frameCounter", "frameTime", "worldTime", "worldDay",
    "moonPhase", "rainStrength", "wetness", "viewWidth", "viewHeight", "aspectRatio", "eyeBrightness",
    "eyeBrightnessSmooth", "eyeAltitude", "isEyeInWater", "nightVision", "blindness", "darknessFactor",
    "darknessLightFactor", "screenBrightness", "fogColor", "skyColor", "fogMode", "fogShape", "fogDensity",
    "fogStart", "fogEnd", "heldItemId", "heldItemId2", "heldBlockLightValue", "heldBlockLightValue2", "entityColor",
    "entityId", "blockEntityId", "currentRenderedItemId", "atlasSize", "terrainTextureSize", "renderStage",
    "centerDepthSmooth", "playerMood", "biome", "biome_category", "biome_precipitation", "temperature", "rainfall",
    "dhNearPlane", "dhFarPlane", "dhRenderDistance", "dhProjection", "dhProjectionInverse",
    "gtexture", "gcolor", "gdepth", "gnormal", "gaux1", "gaux2", "gaux3", "gaux4", "noisetex",
    "depthtex0", "depthtex1", "depthtex2", "shadowtex0", "shadowtex1", "shadowcolor0", "shadowcolor1",
    "watershadow", "dhDepthTex", "dhDepthTex0", "dhDepthTex1",
    *(f"colortex{k}" for k in range(16)),
))
_NOT_A_TYPE = {"return", "else", "case", "in", "out", "inout", "const"}
_ARR = r"(?:\s*\[[^\]]*\])?"
_COMMA_DECL = rf"\b([A-Za-z_]\w*)\s+[A-Za-z_]\w*{_ARR}(?:\s*,\s*[A-Za-z_]\w*{_ARR})*\s*,\s*"


def _norm(path: str) -> str:
    return posixpath.normpath(path)


def _outside(path: str) -> bool:
    """A normalised include path that leaves the repository (never read)."""
    return path.startswith("..") or path.startswith("/") or ":" in path.split("/")[0]


def _pack_root(f: str) -> str | None:
    """The pack's ``shaders/`` directory of a file: the innermost one above it (Iris reads ``/``-rooted includes
    from the pack's own folder, which may itself sit under a ``shaders/`` folder of the repository)."""
    parts = f.split("/")[:-1]
    if "shaders" not in parts:
        return None
    return "/".join(parts[:len(parts) - parts[::-1].index("shaders")])


def shader_files(repo: Path) -> list[str]:
    """The shader files to lint: :data:`SHADER_SUFFIXES`, plus Iris compute stages (``.csh``) under a ``shaders/``
    directory only (elsewhere ``.csh`` is a C-shell script)."""
    return [f for f in _files(repo, SHADER_SUFFIXES + (".csh",))
            if not f.lower().endswith(".csh") or _pack_root(f) is not None]


def _resolve(repo: Path, src: str, kind: str, target: str) -> tuple[str | None, str]:
    """``(path, status)`` of an include target: ``resolved``, ``missing`` or ``external`` (a vanilla import)."""
    parts = src.split("/")
    if kind == "moj_import":
        ns, _, rest = target.rpartition(":")
        ns = ns or "minecraft"
        if "assets" in parts:
            k = parts.index("assets")
            cand = _norm("/".join(parts[:k + 1] + [ns, "shaders", "include", rest]))
            if _outside(cand):
                return cand, "missing"
            if (repo / cand).is_file():
                return cand, "resolved"
            own = parts[k + 1] if len(parts) > k + 1 else None
            return (cand, "missing") if ns == own and ns != "minecraft" else (None, "external")
        return None, "external"
    if target.startswith("/"):
        root = _pack_root(src) or ""
        cand = _norm(posixpath.join(root, target.lstrip("/"))) if root else _norm(target.lstrip("/"))
    else:
        cand = _norm(posixpath.join(posixpath.dirname(src), target))
    if _outside(cand):
        return cand, "missing"
    return cand, "resolved" if (repo / cand).is_file() else "missing"


def _read(repo: Path, f: str, cache: dict) -> tuple[list[str], list[str]] | None:
    if f not in cache:
        try:
            raw = (repo / f).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            cache[f] = None
        else:
            cache[f] = (raw.split("\n"), _strip_comments(raw).split("\n"))
    return cache[f]


def includes(repo: Path, files: list[str] | None = None, _cache: dict | None = None) -> list[dict]:
    """Every ``#include`` / ``#moj_import`` of the shader files as an edge: ``{"from", "line", "target", "to",
    "status"}`` (``resolved``, ``missing``, ``external``)."""
    repo = Path(repo)
    cache = {} if _cache is None else _cache
    out = []
    for f in files if files is not None else shader_files(repo):
        got = _read(repo, f, cache)
        if not got:
            continue
        for k, ln in enumerate(got[1], 1):
            m = _INC.match(ln)
            if m:
                to, status = _resolve(repo, f, m.group(1), m.group(2))
                out.append({"from": f, "line": k, "target": m.group(2), "to": to, "status": status})
    return out


def cycles(edges: list[dict]) -> list[dict]:
    """Include cycles: ``{"files": [a, b, ..., a], "at": "<file>:<line>"}`` (the edge that closes it)."""
    graph: dict[str, list[dict]] = {}
    for e in edges:
        if e["status"] == "resolved":
            graph.setdefault(e["from"], []).append(e)
    out, seen, done = [], set(), set()
    for start in sorted(graph):
        if start in done:
            continue
        path, on_path, stack = [start], {start}, [iter(graph[start])]   # iterative: no depth limit
        while stack:
            e = next(stack[-1], None)
            if e is None:
                stack.pop()
                node = path.pop()
                on_path.discard(node)
                done.add(node)
                continue
            to = e["to"]
            if to in on_path:
                cyc = path[path.index(to):] + [to]
                key = frozenset(cyc)
                if key not in seen:
                    seen.add(key)
                    out.append({"files": cyc, "at": f"{e['from']}:{e['line']}"})
            elif to not in done:
                path.append(to)
                on_path.add(to)
                stack.append(iter(graph.get(to, [])))
    return out


def _brackets(f: str, code: list[str]) -> list[dict]:
    """Bracket balance and ``#if``/``#endif`` pairing of one file, on the first branch of each conditional."""
    out = []
    cond: list[bool] = []          # per open conditional: is the current branch the first one
    cond_lines: list[int] = []
    stack: list[tuple[str, int]] = []
    conditional = False
    continued = False              # the line continues a directive (a multi-line #define): not code
    for k, ln in enumerate(code, 1):
        if continued:
            continued = ln.rstrip().endswith("\\")
            continue
        d = _DIRECTIVE.match(ln)
        if d:
            continued = ln.rstrip().endswith("\\")
            name = d.group(1)
            if name in ("if", "ifdef", "ifndef"):
                cond.append(True)
                cond_lines.append(k)
                conditional = True
            elif name in ("elif", "else"):
                if not cond:
                    out.append(_issue("unbalanced_conditional", f"#{name}", f, k, "verified",
                                      f"#{name} without an #if before it"))
                else:
                    cond[-1] = False
            elif name == "endif":
                if not cond:
                    out.append(_issue("unbalanced_conditional", "#endif", f, k, "verified",
                                      "#endif without an #if before it"))
                else:
                    cond.pop()
                    cond_lines.pop()
            continue
        if not all(cond):
            continue
        for ch in ln:
            if ch in "([{":
                stack.append((ch, k))
            elif ch in _PAIRS:
                if stack and stack[-1][0] == _PAIRS[ch]:
                    stack.pop()
                else:
                    why = (f"`{ch}` closes `{stack[-1][0]}` opened at line {stack[-1][1]}" if stack
                           else f"`{ch}` with nothing open")
                    out.append(_issue("unbalanced_bracket", ch, f, k, "strong_inference" if conditional else "verified",
                                      why))
                    return out
    for ln_no in cond_lines:
        out.append(_issue("unbalanced_conditional", "#if", f, ln_no, "verified", "#if without an #endif"))
    if stack:
        ch, k = stack[-1]
        out.append(_issue("unbalanced_bracket", ch, f, k, "strong_inference" if conditional else "verified",
                          f"`{ch}` is never closed" + (f" ({len(stack)} left open)" if len(stack) > 1 else "")))
    return out


def _version(f: str, code: list[str]) -> list[dict]:
    out, first_code, seen = [], None, None
    for k, ln in enumerate(code, 1):
        if not ln.strip():
            continue
        d = _DIRECTIVE.match(ln)
        if d and d.group(1) == "version":
            if seen:
                out.append(_issue("version_placement", "#version", f, k, "verified",
                                  f"#version written again (first at line {seen})"))
            elif first_code is not None:
                out.append(_issue("version_placement", "#version", f, k, "verified",
                                  f"#version must come first; line {first_code} comes before it"))
            seen = seen or k
        if first_code is None:
            first_code = k
    return out


def _issue(kind: str, name: str, f: str, line: int, status: str, why: str) -> dict:
    return {"kind": kind, "name": name, "at": f"{f}:{line}", "status": status, "why": why}


def _expand(repo: Path, f: str, by_from: dict, cache: dict, seen: set) -> list[tuple[str, int, str, str]]:
    """``(file, line, raw, code)`` for a file with its resolved ``#include``s inlined once each."""
    got = _read(repo, f, cache)
    if not got:
        return []
    seen.add(f)
    out = []
    stack = [(f, iter(enumerate(zip(*got), 1)))]   # iterative: a deep include chain has no depth limit
    while stack:
        cur, lines = stack[-1]
        nxt = next(lines, None)
        if nxt is None:
            stack.pop()
            continue
        k, (raw, code) = nxt
        e = by_from.get((cur, k))
        if e is not None:
            if e["status"] == "resolved" and e["to"] not in seen:
                sub = _read(repo, e["to"], cache)
                if sub:
                    seen.add(e["to"])
                    stack.append((e["to"], iter(enumerate(zip(*sub), 1))))
            if e["status"] != "external":
                continue
        out.append((cur, k, raw, code))
    return out


def _is_stage(f: str) -> bool:
    p = f.split("/")
    return "shaders" in p[:-1] and bool(_STAGE.match(posixpath.splitext(p[-1])[0].lower()))


def _known_macro(name: str) -> bool:
    return name in STANDARD_MACROS or name.startswith(STANDARD_MACRO_PREFIXES)


def _unit_checks(f: str, unit: list[tuple[str, int, str, str]], options: set[str]) -> list[dict]:
    """The checks that need a stage with its includes: undeclared standard uniforms, untested macros, and a
    ``#version`` that an include brings in."""
    out = []
    defined = set(options)
    code_lines = []
    for src, k, _raw, code in unit:
        d = _DIRECTIVE.match(code)
        if d:
            if d.group(1) == "define":
                m = _IDENT.match(d.group(2))
                if m:
                    defined.add(m.group(0))
            elif d.group(1) == "version" and src != f:
                out.append(_issue("version_placement", "#version", src, k, "verified",
                                  f"#version in an included file (included by {f})"))
        else:
            code_lines.append(code)
    text = "\n".join(code_lines)
    for src, k, _raw, code in unit:
        d = _DIRECTIVE.match(code)
        if not d or d.group(1) not in ("ifdef", "ifndef", "if", "elif"):
            continue
        expr = re.sub(r"\b\d\w*", " ", d.group(2))
        for m in _IDENT.finditer(expr):
            n = m.group(0)
            if n != "defined" and n not in defined and not _known_macro(n):
                out.append(_issue("macro_never_defined", n, src, k, "strong_inference",
                                  f"#{d.group(1)} tests {n}, which no #define or //#define option of the pack "
                                  "and no standard Iris/OptiFine macro defines"))
    for n in sorted(STANDARD_UNIFORMS):
        if n not in text:
            continue
        # `<type> n` (a variable, a parameter or a function of that name) or a later name of a comma list
        # (`uniform float viewWidth, viewHeight;`)
        pats = (rf"\b([A-Za-z_]\w*)\s+{n}\s*[;=\[,)(]", _COMMA_DECL + rf"{n}\s*[;=\[,]")
        declared = n in defined or any(dm.group(1) not in _NOT_A_TYPE for pat in pats for dm in re.finditer(pat, text))
        if declared:
            continue
        use = re.compile(rf"(?<![.\w]){n}\b")
        for src, k, _raw, code in unit:
            if not _DIRECTIVE.match(code) and use.search(code):
                out.append(_issue("undeclared_uniform", n, src, k, "strong_inference",
                                  f"{n} is an Iris/OptiFine uniform, but {f} and its includes never declare "
                                  f"`uniform ... {n};`"))
                break
    return out


_GLSLANG_STAGE = {".fsh": "frag", ".frag": "frag", ".vsh": "vert", ".vert": "vert", ".gsh": "geom", ".geom": "geom",
                  ".csh": "comp", ".comp": "comp", ".tcs": "tesc", ".tes": "tese"}
_GLSLANG_ERR = re.compile(r"^ERROR:\s*(?:\d+:)?(\d+):\s*(.*)$")


def glslang(f: str, unit: list[tuple[str, int, str, str]], exe: str) -> list[dict]:
    """Compile one stage (its includes inlined) with ``glslangValidator`` and map each error to its line."""
    stage = _GLSLANG_STAGE.get(posixpath.splitext(f)[1].lower())
    if not stage or not unit:
        return []
    src = "\n".join(raw for _f, _k, raw, _c in unit) + "\n"
    cmd = [exe, "--stdin", "-S", stage, "-DMC_VERSION=12100", "-DIS_IRIS", "-DMC_GL_VERSION=460",
           "-DMC_GLSL_VERSION=460"]
    try:
        p = subprocess.run(cmd, input=src, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return [_issue("glsl_error", "glslangValidator", f, 1, "unknown", f"glslangValidator did not run: {exc}")]
    out = []
    for ln in (p.stdout + "\n" + p.stderr).splitlines():
        m = _GLSLANG_ERR.match(ln.strip())
        if not m:
            continue
        k = int(m.group(1))
        at_f, at_k = (unit[k - 1][0], unit[k - 1][1]) if 0 < k <= len(unit) else (f, 1)
        out.append(_issue("glsl_error", "glslangValidator", at_f, at_k, "verified",
                          m.group(2).strip() + (f" (compiling {f})" if at_f != f else "")))
        if len(out) >= 20:
            break
    return out


def lint(repo: Path, files: list[str] | None = None, *, use_glslang: bool = False) -> dict:
    """``{"files", "stages", "edges", "cycles", "issues", "glslang"}`` for the shader files of the repository."""
    repo = Path(repo)
    files = files if files is not None else shader_files(repo)
    cache: dict = {}
    edges = includes(repo, files, cache)
    cyc = cycles(edges)
    issues = []
    for e in edges:
        if e["status"] == "missing":
            issues.append(_issue("missing_include", e["target"], e["from"], e["line"], "verified",
                                 f"#include names {e['to']}, which "
                                 + ("is outside the repository" if _outside(e["to"]) else "does not exist")))
    for c in cyc:
        issues.append({"kind": "include_cycle", "name": " -> ".join(c["files"]), "at": c["at"], "status": "verified",
                       "why": "the files include each other: " + " -> ".join(c["files"])})
    for f in files:
        got = _read(repo, f, cache)
        if got:
            issues += _version(f, got[1]) + _brackets(f, got[1])
    included = {e["to"] for e in edges if e["status"] == "resolved"}
    stages = [f for f in files if f not in included and _is_stage(f)]
    by_from = {(e["from"], e["line"]): e for e in edges}
    # Per pack, every macro a file of it defines (a `//#define` option toggle or a `#define`): a shared program
    # file tests the stage macros (`#ifdef VSH`) that each sibling stage defines before including it.
    pack_macros: dict[str, set[str]] = {}
    for f in files:
        got = _read(repo, f, cache)
        root = _pack_root(f)
        if got and root is not None:
            names = pack_macros.setdefault(root, set())
            names.update(_OPTION.findall("\n".join(got[0])))
            for ln in got[1]:
                d = _DIRECTIVE.match(ln)
                m = _IDENT.match(d.group(2)) if d and d.group(1) == "define" else None
                if m:
                    names.add(m.group(0))
    exe = (shutil.which("glslangValidator") or shutil.which("glslang")) if use_glslang else None
    for f in stages:
        unit = _expand(repo, f, by_from, cache, set())
        issues += _unit_checks(f, unit, pack_macros.get(_pack_root(f) or "", set()))
        if exe:
            issues += glslang(f, unit, exe)
    uniq, seen = [], set()
    for i in issues:
        key = (i["kind"], i["at"], i["name"], i["why"] if i["kind"] == "glsl_error" else "")
        if key not in seen:
            seen.add(key)
            uniq.append(i)
    note = None if not use_glslang else (f"ran {exe}" if exe else "glslangValidator not found on PATH: not run")
    return {"files": len(files), "stages": len(stages), "edges": edges, "cycles": cyc, "issues": uniq,
            **({"glslang": note} if note else {})}


def render_includes(edges: list[dict]) -> str:
    if not edges:
        return "no #include in the shader files"
    return "\n".join(f"{e['from']}:{e['line']} -> {e['to'] or e['target']}"
                     + ("" if e["status"] == "resolved" else f" ({e['status']})") for e in edges)
