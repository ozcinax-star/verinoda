"""GLSL uniform blocks and the Java that fills them; mirrored constant tables (docs/DESIGN.md D54)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinoda import cli, shaders

GLSL = """#version 330

layout(std140) uniform Frame {
    mat4 ViewProj;
    vec4 Camera;      // x, y, z, time
    vec4 Weather;     // x: rain, y: wetness, z: view distance, w: thunder
    ivec4 Counts;
};

layout(std140) uniform Lights {
    vec4 LightPos[32];
};

#define MAT_DEFAULT 0
#define MAT_STONE 1
#define MAT_WOOD 2
#define MAT_METAL 3

vec3 shade(vec3 c) {
    return c * (1.0 + Weather.y);
}
"""

JAVA = """package com.example.fx;

public final class Engine {
    void upload(ByteBuffer bb, Level level, float t) {
        Std140Builder b = Std140Builder.intoBuffer(bb);
        b.putMat4f(viewProj);
        b.putVec4(cx, cy, cz, t);
        b.putVec4(level.getRainLevel(t), Atmosphere.wetness(), distance * 16f,
                level.getThunderLevel(t));
        b.putIVec4(lights, frame, steps, flags);
    }
}
"""

MATERIAL = """package com.example.fx;

public enum Material {
    DEFAULT(0, 0.8f),
    STONE(1, 0.7f),
    WOOD(2, 0.8f),
    METAL(3, 0.3f);

    public final int id;
    public final float rough;

    Material(int id, float rough) {
        this.id = id;
        this.rough = rough;
    }
}
"""


@pytest.fixture()
def repo(tmp_path) -> Path:
    root = tmp_path / "mod"
    for rel, text in {"src/main/resources/assets/fx/shaders/include/common.glsl": GLSL,
                      "src/client/java/com/example/fx/Engine.java": JAVA,
                      "src/main/java/com/example/fx/Material.java": MATERIAL}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def test_a_field_component_comes_from_the_java_that_fills_it(repo):
    res = shaders.where_from(repo, "Weather.y")
    assert res["status"] == "found" and res["expr"] == "Atmosphere.wetness()" and res["exact"]
    assert res["at"] == "src/client/java/com/example/fx/Engine.java:8"
    assert res["declared_at"] == "src/main/resources/assets/fx/shaders/include/common.glsl:6"
    assert shaders.where_from(repo, "Frame.Camera.w")["expr"] == "t"
    assert shaders.where_from(repo, "LightPos")["status"] == "not_found"   # an array block filled in a loop


def test_the_check_agrees_until_one_side_changes(repo):
    ok = shaders.check(repo)
    assert ok["paired"] == 1 and ok["issues"] == []
    tables = shaders.constant_tables(repo)
    assert tables[0]["prefix"] == "MAT_" and tables[0]["same"] == ["DEFAULT", "METAL", "STONE", "WOOD"]
    # one side changes: a field the Java does not fill, a constant renumbered
    p = repo / "src/main/resources/assets/fx/shaders/include/common.glsl"
    p.write_text(GLSL.replace("    ivec4 Counts;", "    vec4 Fog;\n    ivec4 Counts;").replace("MAT_METAL 3", "MAT_METAL 4"),
                 encoding="utf-8")
    kinds = {(i["kind"], i.get("block") or i.get("name")) for i in shaders.check(repo)["issues"]}
    assert ("block_writer_mismatch", "Frame") in kinds and ("constant_differs", "MAT_METAL") in kinds


def test_cli(repo, capsys):
    assert cli.main(["shader", "Weather.y", "--repo", str(repo)]) == 0
    assert "Atmosphere.wetness()" in capsys.readouterr().out
    assert cli.main(["shader", "--check", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["issues"] == []


def test_analyze_answers_where_a_field_comes_from(repo):
    from verinoda import analysis, workflow
    from verinoda.store import open_store

    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        res = analysis.analyze(st, repo, "where does Weather.y come from?")
    finally:
        st.close()
    claims = {c["id"]: c for c in res["claims"]}
    texts = [claims[c]["text"] for s in res["subquestions"] for c in s["answer_claim_ids"]]
    assert any("`Frame.Weather.y` is filled at src/client/java/com/example/fx/Engine.java:8 by "
               "`Atmosphere.wetness()`" in t for t in texts), texts
