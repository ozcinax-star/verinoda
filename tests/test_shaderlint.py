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
