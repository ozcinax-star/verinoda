"""Minecraft GameTest registry and the tests a change should run (docs/DESIGN.md D49)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import gametests, index, map_text, search_index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

MAIN = "src/main/java/com/example/guard"
GT = "src/gametest/java/com/example/guard/gametest"

FILES = {
    f"{MAIN}/Guard.java": """package com.example.guard;

public final class Guard {
    static int watched;

    public static void tick() {
        watched++;
    }
}
""",
    f"{MAIN}/Story.java": """package com.example.guard;

public final class Story {
    public static void summon() {
    }

    public static void tick() {
        Guard.tick();
    }
}
""",
    f"{MAIN}/Unrelated.java": """package com.example.guard;

public final class Unrelated {
    public static void other() {
    }
}
""",
    f"{GT}/GuardTests.java": """package com.example.guard.gametest;

import com.example.guard.Guard;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.gametest.framework.GameTestHelper;

public final class GuardTests {
    @GameTest
    public void watchesDirectly(GameTestHelper helper) {
        Guard.tick();
        helper.succeed();
    }
}
""",
    f"{GT}/StoryTests.java": """package com.example.guard.gametest;

import com.example.guard.Story;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.gametest.framework.GameTestHelper;

public final class StoryTests {
    @GameTest
    public void summonsAndWaits(GameTestHelper helper) {
        Story.summon();
        helper.succeed();
    }
}
""",
    f"{GT}/ForgottenTests.java": """package com.example.guard.gametest;

import com.example.guard.Guard;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.gametest.framework.GameTestHelper;

public final class ForgottenTests {
    @GameTest
    public void neverRuns(GameTestHelper helper) {
        Guard.tick();
    }
}
""",
    f"{GT}/UnrelatedTests.java": """package com.example.guard.gametest;

import com.example.guard.Unrelated;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.gametest.framework.GameTestHelper;

public final class UnrelatedTests {
    @GameTest
    public void other(GameTestHelper helper) {
        Unrelated.other();
    }
}
""",
    # a literal backslash-n after the object, as a real mod had: the file is still read
    "src/gametest/resources/fabric.mod.json": """{
  "schemaVersion": 1,
  "id": "guard-gametest",
  "entrypoints": {
    "fabric-gametest": [
      "com.example.guard.gametest.GuardTests",
      "com.example.guard.gametest.StoryTests",
      {"value": "com.example.guard.gametest.UnrelatedTests"}
    ]
  }
}\\n
""",
    "build/resources/fabric.mod.json": '{"entrypoints": {"fabric-gametest": ["com.example.Stale"]}}',
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("gametests") / "mod"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    search_index._HANDLES.clear()
    return root


def test_registry_reads_entrypoints_and_skips_build_output(repo):
    reg = gametests.registry(repo)
    assert reg["files"] == ["src/gametest/resources/fabric.mod.json"] and not reg["errors"]
    assert set(reg["classes"]) == {"com.example.guard.gametest.GuardTests", "com.example.guard.gametest.StoryTests",
                                   "com.example.guard.gametest.UnrelatedTests"}
    assert reg["classes"]["com.example.guard.gametest.GuardTests"] == {
        "kind": "server", "at": "src/gametest/resources/fabric.mod.json:6"}


def test_a_change_lists_registered_tests_nearest_first_and_warns_the_unregistered(repo):
    g = index.load(repo)
    seeds = {n for n in g.G.nodes if g.file(n) == f"{MAIN}/Guard.java"}
    res = gametests.for_change(g, seeds)
    reg = [(r["class"], r["distance"]) for r in res["registered"]]
    # GuardTests calls the change; StoryTests calls Story, a class whose tick() calls it (the shared state)
    assert reg == [("GuardTests", 1), ("StoryTests", 2)]
    assert res["registered"][1]["via"] == "Story.tick calls the change"
    assert [r["fqn"] for r in res["unregistered"]] == ["com.example.guard.gametest.ForgottenTests"]
    text = "\n".join(gametests.render(res))
    assert "GameTests to run (registered, nearest first): GuardTests (d1: watchesDirectly); StoryTests" in text
    assert "ForgottenTests" in text and "they never run" in text


def test_impact_view_carries_the_gametests(repo):
    g = index.load(repo)
    v = am.impact(g, [f"{MAIN}/Guard.java"])
    assert [r["class"] for r in v["gametests"]["registered"]] == ["GuardTests", "StoryTests"]
    text = map_text.render({"impact": v}, 40)
    assert "GameTests to run (registered, nearest first): GuardTests" in text


def test_no_gametests_no_section(tmp_path):
    root = tmp_path / "plain"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    g = index.load(root)
    assert gametests.for_change(g, set(g.G.nodes)) is None
    assert "gametests" not in am.impact(g, ["src/a.py"])
