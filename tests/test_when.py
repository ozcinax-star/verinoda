"""When does a method run (docs/DESIGN.md D47): event registrations, lambdas handed to a registration or a
scheduler, and the conditions around each call on the way (verinoda/when.py, index.java_registers_edges)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, index, question_plan, search_index, when, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

PKG = "src/main/java/com/example/watch"

FILES = {
    f"{PKG}/Guard.java": """package com.example.watch;

import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.event.player.UseEntityCallback;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;

public final class Guard {
    private static int ticks;

    public static void initialize() {
        ServerTickEvents.END_SERVER_TICK.register(Guard::tick);
        UseEntityCallback.EVENT.register((player, world, hand, entity, hit) -> {
            onUse(player);
            return null;
        });
    }

    private static void tick(MinecraftServer server) {
        if (server == null) return;
        ticks++;
        if (ticks % 5 == 0 && Settings.on("guard.watch", true)) {
            for (ServerLevel w : server.getAllLevels()) {
                watch(w);
            }
        }
        for (int i = 0; i < 3; i++) {
            if (i == 1) continue;
        }
        other();
    }

    static void watch(ServerLevel w) {
        Scheduler.runLater(80, () -> finish(w));
    }

    static void finish(ServerLevel w) {
    }

    static void onUse(Object player) {
    }

    static void other() {
    }
}
""",
    f"{PKG}/Settings.java": """package com.example.watch;

public final class Settings {
    public static boolean on(String key, boolean dflt) {
        return dflt;
    }
}
""",
    f"{PKG}/Scheduler.java": """package com.example.watch;

public final class Scheduler {
    public static void runLater(int delayTicks, Runnable action) {
    }
}
""",
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("when") / "mod"
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


def _run(repo: Path, symbol: str) -> dict:
    res = when.run(index.load(repo), symbol)
    assert res["status"] == "found", res
    return res


def test_tick_registration_and_conditions(repo):
    res = _run(repo, "Guard.watch")
    first = res["paths"][0]
    ev = next(h for h in first if h.get("event"))
    assert ev["event"] == "at the end of every server tick"
    assert ev["from"] == "Guard.initialize" and ev["to"] == "Guard.tick"
    assert ev["at"].endswith("Guard.java:12")
    call = next(h for h in first if h.get("relation") == "calls")
    assert call["from"] == "Guard.tick" and call["at"].endswith("Guard.java:24")
    # the enclosing if as written (the literal kept), the loop, the early exit above; not the loop that closed
    assert call["conditions"] == ['if ticks % 5 == 0 && Settings.on("guard.watch", true)',
                                  "for each ServerLevel w : server.getAllLevels()",
                                  "not (server == null)"]
    assert call["condition_lines"] == [22, 23, 20]
    text = when.render(res)
    assert "at the end of every server tick" in text and "Guard.java:12" in text


def test_closed_blocks_do_not_guard_a_later_call(repo):
    res = _run(repo, "Guard.other")
    call = next(h for h in res["paths"][0] if h.get("relation") == "calls")
    assert call["conditions"] == ["not (server == null)"]  # not `i == 1` in the loop before it


def test_lambda_registration_names_the_event(repo):
    res = _run(repo, "Guard.onUse")
    ev = next(h for h in res["paths"][0] if h.get("event"))
    assert ev["event"] == "when a player right-clicks an entity"
    assert ev["from"] == "Guard.initialize"
    assert "lambda" in (ev.get("context") or "")


def test_scheduler_delay(repo):
    res = _run(repo, "Guard.finish")
    ev = next(h for h in res["paths"][0] if h.get("event"))
    assert ev["event"] == "80 ticks later"
    assert ev["from"] == "Guard.watch"
    g = index.load(repo)
    d = next(d for _u, _v, d in g.edges({"registers"}) if d.get("delay"))
    assert d["delay"] == "80" and d["lambda"] is True


def test_not_found_and_cli(repo, capsys):
    g = index.load(repo)
    assert when.run(g, "Guard.nothingHere")["status"] == "not_found"
    assert cli.main(["when", "Guard.watch", "--repo", str(repo), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["paths"][0][0]["relation"] == "calls"
    assert all(not k.startswith("_") for p in out["paths"] for h in p for k in h)
    assert cli.main(["when", "Guard.watch", "--repo", str(repo)]) == 0
    assert "at the end of every server tick" in capsys.readouterr().out


@pytest.mark.parametrize("registrar, delay, label", [
    ("ServerTickEvents.END_SERVER_TICK.register", None, "at the end of every server tick"),
    ("ServerLifecycleEvents.SERVER_STARTED.register", None, "once the server has started"),
    ("Scheduler.runLater", "1", "1 tick later"),
    ("Scheduler.runLater", "delay + 20", "after delay + 20 ticks"),
    ("Commands.literal().executes", None, "when the command is run"),
    ("MyBus.SPELL_CAST.register", None, "when spell cast fires"),
    ("Queue.prepare", None, "when prepare(...) calls it back"),
])
def test_event_label(registrar, delay, label):
    assert index.event_label(registrar, delay) == label


def test_guards_braceless_if_and_else():
    code = ["void m() {",
            "    if (a) {",
            "        x();",
            "    } else {",
            "        y();",
            "    }",
            "    if (b)",
            "        z();",
            "}"]
    assert when.guards(code, 1, 3) == ["if a"]
    assert when.guards(code, 1, 5) == ["not (a)"]
    assert when.guards(code, 1, 8) == ["if b"]


@pytest.mark.parametrize("q, intents", [
    ("Guard.watch ne zaman çalışır", ["callers"]),
    ("when does Guard.watch run?", ["callers"]),
    ("what triggers Guard.finish", ["callers"]),
    ("Guard.watch ne zaman eklendi", ["history"]),
    ("when was Guard.watch added", ["history"]),
])
def test_runs_when_questions_ask_for_callers(q, intents):
    assert question_plan.intents_for(q) == intents


def test_analyze_answers_when_from_the_event_and_the_conditions(repo):
    from verinoda import analysis

    st = open_store(repo)
    try:
        res = analysis.analyze(st, repo, "when does Guard.watch run?")
    finally:
        st.close()
    sq = res["subquestions"][0]
    assert sq["intent"] == "callers"
    claims = {c["id"]: c for c in res["claims"]}
    texts = [claims[c]["text"] for c in sq["answer_claim_ids"]]
    assert any("runs at the end of every server tick" in t and "Guard.java:12" in t for t in texts), texts
    assert any('only if ticks % 5 == 0 && Settings.on("guard.watch", true)' in t for t in texts), texts
    assert not any(" registers " in t for t in texts)  # the registration's claim is the event one


def test_first_argument_with_parentheses():
    flat = "Scheduler.runLater(Math.max(1, t - (a ? 0 : 2)), () -> go());"
    i = flat.index("(")
    assert index._first_arg(flat, i, flat.index("() ->")) == "Math.max(1, t - (a ? 0 : 2))"
    assert index.event_label("Scheduler.runLater", "Math.max(1, t - 2)") == "after Math.max(1, t - 2) ticks"
    assert index.event_label("future.thenAccept", None) == "when that future completes"
    assert index.event_label("client.runOnClient", None) == "on the client thread, soon"


def test_mcp_run_when(repo):
    from verinoda.mcp.server import AtlasTools

    t = AtlasTools(repo)
    res = t.run_when("Guard.watch")
    assert res["status"] == "found"
    assert any(h.get("event") == "at the end of every server tick" for h in res["paths"][0])
    assert t.run_when("Guard.nothingHere")["status"] == "not_found"
