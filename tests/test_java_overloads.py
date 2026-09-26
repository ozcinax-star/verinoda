"""Java overloads (docs/DESIGN.md D57): each overload is its own node, a call binds to the overload its
argument count fits, and a name that means several overloads of one class resolves (not a tie)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index, naming, retrieval, search_index, when, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

PKG = "src/main/java/com/example/moon"

FILES = {
    f"{PKG}/Wolf.java": """package com.example.moon;

import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.minecraft.server.MinecraftServer;

public final class Wolf {
    public static void initialize() {
        ServerTickEvents.END_SERVER_TICK.register(Wolf::tick);
    }

    private static void tick(MinecraftServer server) {
        moon(server);
    }

    /** Reads the sky, then turns. */
    private static void moon(MinecraftServer server) {
        moon(server, true, false);
    }

    /** The same rule with the sky handed in. */
    public static void moon(MinecraftServer server, boolean night, boolean full) {
        if (night) {
            turn(server);
        }
    }

    static void turn(MinecraftServer server) {
    }

    static void log(String... parts) {
    }
}
""",
    f"{PKG}/Den.java": """package com.example.moon;

/**
 * The den keeps the pack through the howling nights.
 * Nothing else lives here.
 */
public final class Den {
    static int f0 = 0;
    static int f1 = 1;
    static int f2 = 2;
    static int f3 = 3;
    static int f4 = 4;
    static int f5 = 5;
    static int f6 = 6;
    static int f7 = 7;
    static int f8 = 8;
    static int f9 = 9;
    static int f10 = 10;
    static int f11 = 11;
    static int f12 = 12;
    static int f13 = 13;
    static int f14 = 14;
    static int f15 = 15;
    static int f16 = 16;
    static int f17 = 17;
    static int f18 = 18;
    static int f19 = 19;
    static int f20 = 20;
    static int f21 = 21;
    static int f22 = 22;
    static int f23 = 23;
    static int f24 = 24;
    static int f25 = 25;
    static int f26 = 26;
    static int f27 = 27;
    static int f28 = 28;
    static int f29 = 29;
}
""",
    f"{PKG}/Kennel.java": """package com.example.moon;

import net.minecraft.world.level.saveddata.SavedData;

public final class Kennel extends SavedData {
    private int spot;

    public void onRemoval() {
        remember(3);
    }

    void remember(int x) {
        spot = x;
        setDirty();
    }
}
""",
    f"{PKG}/WolfTests.java": """package com.example.moon;

public final class WolfTests {
    public void turnsUnderTheFullMoon() {
        Wolf.moon(null, true, true);
        Wolf.log("a, b", "c");
    }
}
""",
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("overloads") / "mod"
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


def _moons(g):
    return sorted((n for n in g.G.nodes if g.label(n) == ".moon()"), key=g.line)


def test_each_overload_is_its_own_node_with_its_own_span(repo):
    g = index.load(repo)
    first, second = _moons(g)
    assert g.line(first) < g.line(second)
    assert g.span(first)[1] < g.line(second)  # the first one's lines stop before the second begins
    assert (g.G.nodes[second].get("metadata") or {}).get("arity") == 3
    # the call inside the second overload is its own, not the first one's
    assert any(v.endswith("_turn") for v, _ in g.out_edges(second, {"calls"}))
    assert not any(v.endswith("_turn") for v, _ in g.out_edges(first, {"calls"}))


def test_a_call_binds_to_the_overload_its_arguments_fit(repo):
    g = index.load(repo)
    first, second = _moons(g)
    callers = {n: {g.label(u) for u, _ in g.in_edges(n, {"calls"})} for n in (first, second)}
    assert ".tick()" in callers[first] and ".tick()" not in callers[second]
    # three arguments: the three-parameter overload, in the same file and from another one
    assert {".moon()", ".turnsUnderTheFullMoon()"} <= callers[second]
    assert ".turnsUnderTheFullMoon()" not in callers[first]


def test_an_overloaded_name_resolves_to_the_group(repo):
    g = index.load(repo)
    r = naming.resolve(g, "Wolf.moon")
    assert r.status == naming.EXACT and len(r.overloads) == 2 and r.node == r.overloads[0]
    assert "2 overloads" in r.note


def test_when_and_trace_follow_every_overload(repo):
    g = index.load(repo)
    res = when.run(g, "Wolf.moon")
    assert res["status"] == "found" and res.get("resolution_note")
    heads = [next((h.get("event") for h in p if h.get("event")), None) or p[-1].get("entry") for p in res["paths"]]
    assert any(h and "tick" in h for h in heads)
    assert any(h and "turnsUnderTheFullMoon" in h for h in heads)
    tr = retrieval.trace(g, "WolfTests.turnsUnderTheFullMoon", "Wolf.moon")
    assert tr["status"] == "found"


def test_call_argument_count():
    c = index._call_arg_count
    assert c("a.f()", 3) == 0
    assert c("a.f(x, g(1, 2))", 3) == 2
    assert c('a.f(x, "a,b")', 3) == 2
    assert c("a.f('(')", 3) == 1
    assert c("a.f(x,", 3) is None


def test_a_class_doc_above_its_declaration_prints_where_it_is(repo):
    import re

    g = index.load(repo)
    text = retrieval.render_text(retrieval.retrieve(g, "which class keeps the pack through the howling nights"))
    assert "howling nights" in text
    for a, b in re.findall(r"Den\.java:(\d+)-(\d+)", text):
        assert int(a) <= int(b), text  # never an inverted, empty window


def test_how_does_it_work_with_one_subject_is_answered_by_its_calls_as_an_inference():
    """"How does X move each tick?" names one place and no storage: what X calls answers it, never fully
    (it used to look for entry -> persistence paths and matched unrelated ones by a shared label)."""
    from verinoda import analysis

    sq = {"id": "q1", "intent": "flow", "done_when": {"kind": "path_found"}}
    rows = [{"id": "c1", "kind": "relation", "status": "statically_verified"}]
    assert analysis.judge(sq, rows, {}) == "unmet"
    assert analysis.judge(sq, rows, {"mechanism": True}) == "met_with_inference"
    assert analysis.answer_claims(sq, rows, {"mechanism": True}) == ["c1"]


def test_what_a_change_breaks_names_the_callers_of_the_named_method(repo):
    from verinoda import analysis

    st = open_store(repo)
    try:
        res = analysis.analyze(st, repo, "what breaks if I change the signature of Wolf.turn?")
    finally:
        st.close()
    texts = [c["text"] for c in res["claims"] if c["text"].startswith("A change to")]
    assert any("`Wolf.turn` reaches the code that calls it: `Wolf.moon`" in t and "Wolf.java:23" in t
               for t in texts), texts


def test_paths_through_the_code_a_question_is_about(repo):
    """The repository-wide entry -> storage view is capped; a question's own code gets its paths: forward to the
    nearest write, back to an entry point or a caller nothing in the project calls."""
    from verinoda import architecture_map as am

    g = index.load(repo)
    kennel = next(n for n in g.G.nodes if g.label(n) == "Kennel")
    paths = am.paths_through(g, [kennel])
    assert paths and [h["to"] for h in paths[0]["hops"]] == [".remember()"], paths
    assert paths[0]["hops"][0]["from"] == ".onRemoval()"


def test_overload_group_and_the_targets_of_a_named_method(repo):
    g = index.load(repo)
    first, second = _moons(g)
    assert index.overload_group(g, second) == [first, second]
    turn = next(n for n in g.G.nodes if g.label(n) == ".turn()")
    assert index.overload_group(g, turn) == [turn]  # not overloaded: itself
