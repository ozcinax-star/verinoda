"""Shaderpack lint and the #include graph: includes resolved with their lines, cycles, brackets, #version, the
standard Iris/OptiFine uniforms and macros, and glslangValidator only when asked for and installed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinoda import cli, shaderlint, shaders

COMMON = """#ifndef LIB_COMMON
#define LIB_COMMON

//#define SHADOWS
#define FOG_DENSITY 0.5

float fogAmount(float d) {
    return 1.0 - exp(-d * FOG_DENSITY);
}
#endif
"""

LIGHT = """#include "common.glsl"

vec3 sunDir() {
    return normalize(sunPosition);
}
"""

TERRAIN = """#version 330 compatibility

uniform vec3 sunPosition;
uniform float frameTimeCounter;

#include "/lib/light.glsl"

#if MC_VERSION >= 11300 && defined(IS_IRIS)
#endif

void main() {
#ifdef SHADOWS
    if (frameTimeCounter > 1.0) {
#else
    if (frameTimeCounter > 2.0) {
#endif
        gl_FragData[0] = vec4(sunDir() * fogAmount(1.0), 1.0);
    }
}
"""


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@pytest.fixture()
def pack(tmp_path) -> Path:
    return _write(tmp_path / "pack", {"shaders/lib/common.glsl": COMMON, "shaders/lib/light.glsl": LIGHT,
                                      "shaders/gbuffers_terrain.fsh": TERRAIN})


def test_a_clean_pack_has_its_include_edges_and_no_issue(pack):
    res = shaderlint.lint(pack)
    edges = {(e["from"], e["line"]): (e["to"], e["status"]) for e in res["edges"]}
    assert edges == {("shaders/gbuffers_terrain.fsh", 6): ("shaders/lib/light.glsl", "resolved"),
                     ("shaders/lib/light.glsl", 1): ("shaders/lib/common.glsl", "resolved")}
    assert res["stages"] == 1 and res["cycles"] == [] and res["issues"] == [], res["issues"]


def _kinds(res):
    return {(i["kind"], i["name"], i["at"].rsplit(":", 1)[0], int(i["at"].rsplit(":", 1)[1]), i["status"])
            for i in res["issues"]}


def test_a_brace_left_open_is_reported_with_its_line(pack):
    (pack / "shaders/lib/common.glsl").write_text(COMMON.replace("float fogAmount(float d) {",
                                                                 "float fogAmount(float d) {\n    if (d > 0.0) {"),
                                                  encoding="utf-8")
    assert ("unbalanced_bracket", "{", "shaders/lib/common.glsl", 7, "strong_inference") in _kinds(
        shaderlint.lint(pack))   # the function's brace; the file has an #ifndef guard: the first branch was read
    (pack / "shaders/lib/light.glsl").write_text(LIGHT.replace("normalize(sunPosition);", "normalize(sunPosition;"),
                                                 encoding="utf-8")
    found = [i for i in shaderlint.lint(pack)["issues"] if i["at"] == "shaders/lib/light.glsl:5"]
    assert found and found[0]["status"] == "verified" and "closes `(` opened at line 4" in found[0]["why"]


def test_version_include_and_conditional_errors(pack):
    _write(pack, {"shaders/lib/common.glsl": "#version 120\n" + COMMON.replace("#endif\n", ""),
                  "shaders/composite.fsh": "uniform float viewWidth;\n#version 330\n#include \"lib/nowhere.glsl\"\n"
                                           "void main() { gl_FragColor = vec4(viewWidth); }\n"})
    k = _kinds(shaderlint.lint(pack))
    assert ("version_placement", "#version", "shaders/composite.fsh", 2, "verified") in k
    assert ("version_placement", "#version", "shaders/lib/common.glsl", 1, "verified") in k   # in an include
    assert ("missing_include", "lib/nowhere.glsl", "shaders/composite.fsh", 3, "verified") in k
    assert ("unbalanced_conditional", "#if", "shaders/lib/common.glsl", 2, "verified") in k


def test_an_include_cycle(pack):
    (pack / "shaders/lib/common.glsl").write_text('#include "light.glsl"\n' + COMMON, encoding="utf-8")
    res = shaderlint.lint(pack)
    assert res["cycles"] and set(res["cycles"][0]["files"]) == {"shaders/lib/common.glsl", "shaders/lib/light.glsl"}
    assert any(i["kind"] == "include_cycle" for i in res["issues"])


def test_undeclared_uniform_and_undefined_macro(pack):
    (pack / "shaders/gbuffers_terrain.fsh").write_text(
        TERRAIN.replace("uniform vec3 sunPosition;\n", "").replace("#ifdef SHADOWS", "#ifdef SHADOWS_TYPO"),
        encoding="utf-8")
    k = _kinds(shaderlint.lint(pack))
    assert ("undeclared_uniform", "sunPosition", "shaders/lib/light.glsl", 4, "strong_inference") in k
    assert ("macro_never_defined", "SHADOWS_TYPO", "shaders/gbuffers_terrain.fsh", 11, "strong_inference") in k
    assert not any(n in ("MC_VERSION", "IS_IRIS", "SHADOWS") for _k, n, _f, _l, _s in k)   # standard or an option


def test_moj_import_resolves_in_the_mod_and_vanilla_is_external(tmp_path):
    root = _write(tmp_path / "mod", {
        "assets/fx/shaders/include/fog.glsl": "vec4 fog() { return vec4(0.0); }\n",
        "assets/fx/shaders/core/sky.fsh": "#version 150\n#moj_import <fx:fog.glsl>\n#moj_import <fog.glsl>\n"
                                          "#moj_import <fx:gone.glsl>\nvoid main() {}\n"})
    st = {e["target"]: e["status"] for e in shaderlint.includes(root)}
    assert st == {"fx:fog.glsl": "resolved", "fog.glsl": "external", "fx:gone.glsl": "missing"}


def test_cli_check_reports_the_error_line_and_includes(pack, capsys):
    (pack / "shaders/lib/light.glsl").write_text(LIGHT.replace("}\n", ""), encoding="utf-8")
    assert cli.main(["shader", "--check", "--repo", str(pack)]) == 3
    out = capsys.readouterr().out
    assert "unbalanced_bracket: { (shaders/lib/light.glsl:3)" in out and "2 include(s)" in out
    assert cli.main(["shader", "--includes", "--repo", str(pack), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["edges"]) == 2


def test_glslang_is_opt_in_and_maps_errors_back(pack, monkeypatch):
    calls = []
    files = shaders._files(pack, shaders.SHADER_SUFFIXES)   # listed before subprocess.run is replaced

    class Done:
        stdout = "ERROR: 0:9: 'sunDir' : no matching overloaded function found\n"
        stderr = ""

    monkeypatch.setattr(shaderlint.shutil, "which", lambda name: "glslangValidator" if name == "glslangValidator"
                        else None)
    monkeypatch.setattr(shaderlint.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw["input"])) or Done())
    assert "glslang" not in shaderlint.lint(pack, files) and calls == []   # off unless asked for
    res = shaderlint.lint(pack, files, use_glslang=True)
    assert len(calls) == 1 and "-S" in calls[0][0] and "frag" in calls[0][0]
    unit_line_9 = calls[0][1].split("\n")[8]
    err = [i for i in res["issues"] if i["kind"] == "glsl_error"]
    assert err and err[0]["status"] == "verified"
    f, line = err[0]["at"].rsplit(":", 1)
    assert (pack / f).read_text(encoding="utf-8").split("\n")[int(line) - 1] == unit_line_9
    monkeypatch.setattr(shaderlint.shutil, "which", lambda name: None)
    assert "not found" in shaderlint.lint(pack, files, use_glslang=True)["glslang"]


def _lint_one(tmp_path, files):
    root = _write(tmp_path / "p", files)
    return _kinds(shaderlint.lint(root)), root


def test_comma_lists_and_functions_declare_standard_names(tmp_path):
    k, _ = _lint_one(tmp_path, {"shaders/composite.fsh":
                                "#version 330\nuniform float viewWidth, viewHeight;\n"
                                "uniform sampler2D colortex0 , colortex1;\nvec3 skyColor(vec3 d) { return d; }\n"
                                "void main(){ gl_FragColor = texture2D(colortex1, vec2(viewHeight)) "
                                "+ vec4(skyColor(vec3(1.0)), 1.0); }\n"})
    assert not any(kind == "undeclared_uniform" for kind, *_ in k), k


def test_stage_macros_defined_by_sibling_stages_are_defined_in_the_pack(tmp_path):
    k, _ = _lint_one(tmp_path, {
        "shaders/gbuffers_basic.fsh": '#version 330\n#define FSH\n#include "/program/basic.glsl"\n',
        "shaders/gbuffers_basic.vsh": '#version 330\n#define VSH\n#include "/program/basic.glsl"\n',
        "shaders/program/basic.glsl": "#ifdef FSH\nvoid main(){}\n#endif\n#ifdef VSH\nvoid main(){}\n#endif\n"
                                      "#ifdef NOWHERE\n#endif\n"})
    assert {(kind, n) for kind, n, *_ in k} == {("macro_never_defined", "NOWHERE")}


def test_a_csh_outside_shaders_is_a_shell_script(tmp_path):
    root = _write(tmp_path / "p", {"scripts/setup.csh": "#!/bin/csh\n# if the user has no path, set it\n",
                                   "shaders/compute.csh": "#version 430\nvoid main() {\n"})
    res = shaderlint.lint(root)
    assert res["files"] == 1 and {i["at"] for i in res["issues"]} == {"shaders/compute.csh:2"}


def test_rooted_include_reads_the_innermost_shaders_directory(tmp_path):
    root = _write(tmp_path / "p", {"shaders/MyPack/shaders/composite.fsh": '#version 330\n#include "/lib/a.glsl"\n',
                                   "shaders/MyPack/shaders/lib/a.glsl": "float a() { return 1.0; }\n"})
    res = shaderlint.lint(root)
    assert [e["to"] for e in res["edges"]] == ["shaders/MyPack/shaders/lib/a.glsl"] and res["issues"] == []


def test_define_continuation_lines_are_not_code(tmp_path):
    k, _ = _lint_one(tmp_path, {"shaders/composite.fsh": "#version 330\n#define F(x) (x + \\n   1.0)\n"
                                                         "#define BODY(a) { \\n  a; }\n"
                                                         "void main(){ gl_FragColor = vec4(F(1.0)); }\n"})
    assert k == set(), k


def test_a_bom_does_not_hide_the_first_directive(tmp_path):
    root = tmp_path / "p"
    (root / "shaders/lib").mkdir(parents=True)
    (root / "shaders/lib/bom.glsl").write_bytes(b"\xef\xbb\xbf#ifdef FOO\n#define FOO\n#endif\n")
    assert shaderlint.lint(root)["issues"] == []


def test_moj_import_outside_the_repository_is_not_resolved(tmp_path):
    root = _write(tmp_path / "mod", {"assets/m/shaders/core/x.fsh":
                                     "#version 150\n#moj_import <../../../../../x.ini>\n"})
    (tmp_path / "x.ini").write_text("[fonts]\n", encoding="utf-8")
    (e,) = shaderlint.includes(root)
    assert e["status"] == "missing"
    unit = shaderlint._expand(root, "assets/m/shaders/core/x.fsh", {(e["from"], e["line"]): e}, {}, set())
    assert {f for f, *_ in unit} == {"assets/m/shaders/core/x.fsh"} and "[fonts]" not in [r for _f, _k, r, _c in unit]
    assert "outside the repository" in shaderlint.lint(root)["issues"][0]["why"]


def test_a_deep_include_chain_does_not_overflow(tmp_path):
    edges = [{"from": f"f{i}", "to": f"f{i + 1}", "line": 1, "status": "resolved"} for i in range(3000)]
    assert shaderlint.cycles(edges + [{"from": "f3000", "to": "f0", "line": 1, "status": "resolved"}])
    files = {f"shaders/lib/f{i}.glsl": f'#include "f{i + 1}.glsl"\n' for i in range(1500)}
    root = _write(tmp_path / "p", {**files, "shaders/lib/f1500.glsl": "float z;\n"})
    by_from = {(e["from"], e["line"]): e for e in shaderlint.includes(root)}
    unit = shaderlint._expand(root, "shaders/lib/f0.glsl", by_from, {}, set())
    assert ("shaders/lib/f1500.glsl", 1, "float z;", "float z;") in unit


def test_glslang_keeps_errors_on_one_line_and_reads_utf8(pack, monkeypatch):
    files = shaders._files(pack, shaders.SHADER_SUFFIXES)
    seen = {}

    class Done:
        stdout = "ERROR: 0:2: a : undeclared identifier\nERROR: 0:2: b : undeclared identifier\n"
        stderr = ""

    monkeypatch.setattr(shaderlint.shutil, "which", lambda name: "glslangValidator" if name == "glslangValidator"
                        else None)
    monkeypatch.setattr(shaderlint.subprocess, "run", lambda cmd, **kw: seen.update(kw) or Done())
    err = [i["why"] for i in shaderlint.lint(pack, files, use_glslang=True)["issues"] if i["kind"] == "glsl_error"]
    assert err == ["a : undeclared identifier", "b : undeclared identifier"]
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
