"""Stack traces and GameTest results of a log mapped onto the code (docs/DESIGN.md D53)."""

from __future__ import annotations

import json
import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, index, search_index, trace_log, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

FILES = {
    "src/main/java/com/example/guard/Guard.java": """package com.example.guard;

public final class Guard {
    public void remove(int reason) {
        log(reason);
    }

    void log(int reason) {
    }
}
""",
    "src/gametest/java/com/example/guard/gametest/GuardTests.java": """package com.example.guard.gametest;

import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.gametest.framework.GameTestHelper;

public final class GuardTests {
    @GameTest
    public void watchesTheGate(GameTestHelper helper) {
        helper.succeed();
    }

    @GameTest
    public void neighbourSucceeds(GameTestHelper helper) {
        helper.succeed();
    }
}
""",
}

LOG = """[14:02:08] [Server thread/INFO] (Minecraft) guardtests.watchesthegate passed! (1800 ms)
[14:02:11] [Server thread/INFO] (guard) [GUARDREMOVE] DISCARDED guard body 1b2c
java.lang.Throwable
\tat com.example.guard.Guard.remove(Guard.java:5)
\tat net.minecraft.world.entity.Entity.discard(Entity.java:3150)
\tat net.minecraft.gametest.framework.GameTestInfo.lambda$succeed$2(GameTestInfo.java:260)
\tat java.base/java.util.ArrayList.forEach(ArrayList.java:1596)
\tat net.minecraft.gametest.framework.GameTestInfo.succeed(GameTestInfo.java:255)
\tat net.minecraft.gametest.framework.GameTestHelper.succeed(GameTestHelper.java:880)
[14:02:11] [Server thread/INFO] (Minecraft) guardtests.neighboursucceeds passed! (2150 ms)
[14:02:12] [Server thread/ERROR] (Minecraft) java.lang.IllegalStateException: gate closed
\tat com.example.guard.Guard.log(Guard.java:9)
\tat com.example.guard.Guard.remove(Guard.java:5)
\t... 12 more
"""


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("tracelog") / "mod"
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


def test_parse_traces_and_results():
    traces, results = trace_log.parse(LOG)
    assert [t.marker for t in traces] == ["[GUARDREMOVE] DISCARDED guard body 1b2c", None]
    assert len(traces[0].frames) == 6                     # `java.base/...` is a frame, not a new trace
    assert traces[1].header == "java.lang.IllegalStateException: gate closed"
    assert [(r["name"], r["outcome"]) for r in results] == [("guardtests.watchesthegate", "passed"),
                                                            ("guardtests.neighboursucceeds", "passed")]


def test_a_trace_through_succeed_is_tied_to_the_test_that_finished(repo):
    res = trace_log.analyze(index.load(repo), LOG, source="latest.log")
    t = res["traces"][0]
    assert t["gametest"]["via"] == "GameTestInfo.succeed"
    assert t["gametest"]["test"] == "GuardTests.neighbourSucceeds"
    assert "same time" in t["gametest"]["basis"]
    project = [f for f in t["frames"] if f.get("node")]
    assert [f["node"] for f in project] == ["Guard.remove"]
    folded = next(f for f in t["frames"] if "folded" in f)
    assert folded["folded"] == 5 and "GameTestInfo.succeed" in folded["notable"]
    second = res["traces"][1]
    assert [f["node"] for f in second["frames"] if f.get("node")] == ["Guard.log", "Guard.remove"]
    assert "gametest" not in second
    text = trace_log.render(res)
    assert "via GameTestInfo.succeed: the test GuardTests.neighbourSucceeds" in text


def test_cli_stores_claims_with_the_log_as_evidence(repo, tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text(LOG, encoding="utf-8")
    assert cli.main(["trace-log", str(log), "--repo", str(repo), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["claims"] and all(c["status"] in ("observed", "strong_inference", "weak_inference", "unknown") for c in out["claims"])
    assert "neighbourSucceeds" in out["claims"][0]["text"]
    copies = list((repo / ".verinoda" / "logs").glob("latest-*.log"))
    assert len(copies) == 1                               # a log outside the repository is kept to be cited
    assert cli.main(["trace-log", str(log), "--repo", str(repo), "--no-store"]) == 0
    assert "stored" not in capsys.readouterr().out
