"""JVM callbacks (docs/DESIGN.md D38): a method reference passed as an argument is a
``registers`` edge, never a ``calls`` edge. trace, impact, review and the UI follow it, labelled; the
ranking does not weigh it; a claim "A calls B" that only a method reference supports is not a call; the
map knows the entry points of Fabric / NeoForge mods and their JVM persistence sinks.

The faults of the reverted first attempt (git show 3c066ec) each have a test here: a method reference
does not suppress a later direct call, ``this::m`` inside an anonymous class or a Kotlin lambda / object
expression is not the outer class, a Java text block is not code, and the claim grade.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import entail, index, retrieval, search_index, workflow  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FORGE = ROOT / "examples" / "forge_mod"
GLOW = ROOT / "examples" / "glow_mod"
TICK = "src/main/java/com/example/glowmod/repair/RepairScheduler.java"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _scan(repo: Path, *, git: bool = True) -> Path:
    if git:
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    search_index._HANDLES.clear()
    return repo


def _copy(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    return _scan(dst)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _regs(g: index.Graph) -> set[tuple[str, str]]:
    return {(_name(g, u), _name(g, v)) for u, v, _d in g.edges({"registers"})}


def _name(g: index.Graph, n: str) -> str:
    cls = next((g.label(c) for c, _ in g.in_edges(n, {"method"})), "")
    return f"{cls}.{g.label(n).strip('.()')}" if cls else g.label(n).strip(".()")


@pytest.fixture(scope="module")
def forge(tmp_path_factory):
    return _copy(FORGE, tmp_path_factory.mktemp("jvmcb") / "forge_mod")


@pytest.fixture(scope="module")
def glow(tmp_path_factory):
    return _copy(GLOW, tmp_path_factory.mktemp("jvmcb") / "glow_mod")


# -- the edges on the example mods -------------------------------------------------------------------

def test_method_references_on_the_example_mods_are_registers_edges(forge, glow):
    fg, gg = index.load(forge), index.load(glow)
    assert _regs(fg) == {("EmberForgeBlock.getTicker", "EmberForgeBlockEntity.serverTick"),
                         ("EmberNetwork.register", "EmberNetwork.handleStoke")}
    assert _regs(gg) == {("RepairScheduler.register", "RepairScheduler.tick"),
                         ("RepairScheduler.register", "RepairScheduler.temizle"),
                         ("GlowModClient.onInitializeClient", "GlowModClient.onEndTick"),
                         ("WispSpawner.register", "WispSpawner.onChunkLoad"),
                         ("ServerNetworking.register", "ServerNetworking.onSummonWisp"),
                         ("GlowCommands.register", "GlowCommands.wisp"),
                         ("GlowCommands.register", "GlowCommands.ritual"),
                         ("GlowCommands.register", "GlowCommands.reload")}
    d = next(d for _u, v, d in gg.edges({"registers"}) if gg.label(v) == ".tick()")
    assert d["confidence"] == "INFERRED" and d["_origin"] == index.JAVA_REFS_ORIGIN
    assert (d["source_file"], d["source_location"]) == (TICK, "L24")
    assert d["registrar"] == "ServerTickEvents.END_SERVER_TICK.register"
    assert d["context"] == "RepairScheduler::tick passed to ServerTickEvents.END_SERVER_TICK.register(...)"
    # never a call: the method reference adds no calls edge
    assert not any(gg.label(v) == ".tick()" and gg.file(v) == TICK for _u, v, _d in gg.edges({"calls"}))
    # stored with the receiver-call sidecar (kept apart from the call edges) and applied from it
    side = index._read_sidecar(glow)
    assert side["version"] == index.RECEIVER_SIDECAR_VERSION and len(side["registers"]) == 8
    assert not any(d.get("relation") == "registers" for _u, _v, d in side["edges"])


def test_the_ranking_does_not_weigh_registers_edges(glow):
    assert "registers" not in search_index.REL_WEIGHTS and "registers" not in retrieval.EXPAND_RELATIONS
    g = index.load(glow)
    plain = index.load(glow)
    plain.G.remove_edges_from([(u, v, k) for u, v, k, d in plain.G.edges(keys=True, data=True)
                               if d.get("relation") == "registers"])
    plain.__dict__.pop("_ppr_adj", None)
    for q in ("how does the repair scheduler put blocks back every tick", "summon wisp packet handler"):
        a = [(h.key, round(h.score, 9)) for h in search_index.rank(g, q, limit=15).hits]
        b = [(h.key, round(h.score, 9)) for h in search_index.rank(plain, q, limit=15).hits]
        assert a == b


# -- trace ---------------------------------------------------------------------------------------------

def test_trace_follows_a_callback_only_when_no_call_path_exists(forge, glow):
    fg, gg = index.load(forge), index.load(glow)
    for g, s, t, at in ((fg, "EmberForgeBlock.getTicker", "EmberForgeBlockEntity.serverTick",
                         "src/main/java/net/ashvale/emberforge/block/EmberForgeBlock.java:72"),
                        (gg, "RepairScheduler.register", "RepairScheduler.tick", f"{TICK}:24")):
        before = retrieval.trace(g, s, t, callbacks=False)
        assert before["status"] == "no directed path"
        res = retrieval.trace(g, s, t)
        assert res["status"] == "found" and len(res["paths"]) == 1
        [hop] = res["paths"][0]
        assert (hop["relation"], hop["kind"], hop["confidence"], hop["at"]) == ("registers", "callback", "INFERRED", at)
        assert hop["derived_by"] == index.JAVA_REFS_ORIGIN and "passed to" in hop["context"]
        assert res["note"].startswith("a path includes a callback hop (registers)")
        anyres = retrieval.trace(g, s, t, mode="any")
        assert anyres["status"] == "found" and anyres["reachability"] == "callback"
    # a path that exists without callbacks comes back exactly as before
    same = retrieval.trace(gg, "GlowMod.onInitialize", "RepairScheduler.register")
    assert same == retrieval.trace(gg, "GlowMod.onInitialize", "RepairScheduler.register", callbacks=False)
    assert same["status"] == "found" and "note" not in same
    # a call path, then the callback
    long = retrieval.trace(gg, "GlowMod.onInitialize", "RepairScheduler.tick")
    assert [h["relation"] for h in long["paths"][0]] == ["calls", "registers"]


def test_the_cli_trace_labels_the_callback_hop(glow, capsys):
    from verinoda import cli

    code = cli.main(["trace", "RepairScheduler.register", "RepairScheduler.tick", "--repo", str(glow)])
    out = capsys.readouterr().out
    assert code == 0
    assert "-registers[INFERRED]-> .tick()" in out
    assert "(callback: RepairScheduler::tick passed to ServerTickEvents.END_SERVER_TICK.register(...))" in out


# -- impact: the map, the UI, review ------------------------------------------------------------------

def test_impact_reaches_the_method_that_registers_a_callback(forge, glow):
    gg = index.load(glow)
    iv = am.impact(gg, ["RepairScheduler.tick"])
    reg = next(a for a in iv["affected_symbols"] if a["symbol"] == ".register()" and a["at"] == f"{TICK}:23")
    assert reg["distance"] == 1 and reg["via"] == am.CALLBACK_VIA
    assert "callback registrations" in iv["coverage"]["method"]
    # what was found without callbacks keeps its distance and has no mark
    cls = next(a for a in iv["affected_symbols"] if a["symbol"] == "RepairScheduler")
    assert cls["distance"] == 1 and "via" not in cls
    fg = index.load(forge)
    fv = am.impact(fg, ["EmberNetwork.handleStoke"])
    assert any(a["symbol"] == ".register()" and a.get("via") == am.CALLBACK_VIA for a in fv["affected_symbols"])


def test_ui_impact_lists_the_registering_method(glow):
    from verinoda.ui import data as uidata

    a = uidata.Atlas(glow)
    a.ensure()
    tick = next(r for r in a.search("RepairScheduler.tick()")["results"] if r["title"] == "RepairScheduler.tick()")
    r = a.impact(tick["id"], 3)
    reg = next(x for x in r["items"] if x["title"] == "RepairScheduler.register()")
    assert reg["depth"] == 1 and reg["relation"] == "registers (callback)" and reg["via"] == tick["id"]
    assert reg["at"] == f"{TICK}:24"


def test_review_dependents_follow_the_callback(glow, tmp_path):
    from verinoda import review as rv

    repo = tmp_path / "glow"
    shutil.copytree(glow, repo)
    p = repo / TICK
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("int budget = GlowConfig.get().blocksPerTick();",
                              "int budget = GlowConfig.get().blocksPerTick() * 2;"), encoding="utf-8", newline="\n")
    st = open_store(repo)
    try:
        res = rv.review(repo, store=st)
    finally:
        st.close()
    dep = next(d for d in res["dependents"] if d["symbol"].endswith("::RepairScheduler.register"))
    assert dep["distance"] == 1 and dep["via"][0]["relation"] == "registers (callback)"
    assert dep["via"][0]["at"] == f"{TICK}:24"


# -- the map: entry points and sinks of JVM mods -----------------------------------------------------

def test_framework_entry_points_of_a_fabric_mod(glow):
    df = am.dataflow(index.load(glow))
    entries = {e["symbol"] + "@" + e["at"].rsplit("/", 1)[-1]: e for e in df["entries"]}
    init = entries[".onInitialize()@GlowMod.java:19"]
    assert init["basis"] == "declared"
    assert any('fabric.mod.json entrypoint "main"' in w for w in init["why"])
    assert any("implements ModInitializer" in w for w in init["why"])
    assert entries[".onInitializeClient()@GlowModClient.java:18"]["basis"] == "declared"
    tick = entries[".tick()@RepairScheduler.java:33"]
    assert tick["basis"] == "framework"
    assert tick["why"] == [f"callback registered with ServerTickEvents.END_SERVER_TICK.register(...) at {TICK}:24"]
    assert entries[".onSummonWisp()@ServerNetworking.java:20"]["basis"] == "callback"
    # declared first, then framework, then callbacks, then the name heuristics
    tiers = [am.ENTRY_TIERS.get(e.get("basis"), 3) for e in df["entries"]]
    assert tiers == sorted(tiers)
    assert not any("gametest" in e["at"] for e in df["entries"])   # a test source set is no entry point
    # the dataflow view is no longer empty: onInitialize -> GlowConfig.load writes the config file
    path = next(p for p in df["paths"] if p["entry"].endswith("GlowMod.java:19"))
    assert [h["to"] for h in path["hops"]] == [".load()"] and path["sink_kinds"] == ["file-write"]
    assert any("JVM mods" in lim for lim in df["coverage"]["limits"])


def test_framework_entry_points_of_a_neoforge_mod(forge):
    df = am.dataflow(index.load(forge))
    by = {(e["symbol"], e["at"].rsplit("/", 1)[-1]): e for e in df["entries"]}
    assert by[(".EmberForge()", "EmberForge.java:21")]["why"] == ["@Mod class: the mod loader constructs it"]
    click = by[(".onLeftClickForge()", "ModEvents.java:26")]
    assert click["basis"] == "framework"
    assert click["why"] == ["@SubscribeEvent handler of PlayerInteractEvent.LeftClickBlock"]
    assert by[("ModEvents", "ModEvents.java:15")]["basis"] == "framework"   # @EventBusSubscriber
    assert by[(".serverTick()", "EmberForgeBlockEntity.java:75")]["basis"] == "callback"
    sinks = {s["symbol"]: s for s in df["sinks"]}
    assert sinks[".stoke()"]["evidence"][0]["kind"] == "saved-data-write (dirty flag)"
    assert sinks[".stoke()"]["evidence"][0]["derived_by"] == "architecture_map.JVM_SINK_PATTERNS (heuristic)"
    ends = {(p["entry"].rsplit("/", 1)[-1], p["sink"].rsplit("/", 1)[-1]) for p in df["paths"]}
    assert ("EmberNetwork.java:28", "EmberForgeBlockEntity.java:140") in ends   # handleStoke -> stoke


def test_entry_points_and_sinks_from_a_small_fabric_fixture(tmp_path):
    root = tmp_path / "fab"
    _write(root, "src/main/resources/fabric.mod.json",
           '{"id": "fab", "entrypoints": {"main": ["com.fab.Boot"], "fabric-datagen": ["com.fab.Gen::run"]}}\n')
    _write(root, "src/main/java/com/fab/Boot.java", """package com.fab;

import java.util.List;

public class Boot {
    private final List<String> items = List.of();

    public void onInitialize() {
        Store.save("x");
        items.forEach(this::handle);
        Loader.BUS.addListener(this::setup);
    }

    void handle(String s) {
    }

    void setup(Object event) {
    }
}
""")
    _write(root, "src/main/java/com/fab/Gen.java", """package com.fab;

public class Gen {
    public static void run() {
    }
}
""")
    _write(root, "src/main/java/com/fab/Store.java", """package com.fab;

import java.nio.file.Files;
import java.nio.file.Path;

public class Store {
    public static void save(String s) throws Exception {
        Files.writeString(Path.of("fab.txt"), s);
    }

    public static String help() {
        return "call Files.writeString(path, s) to save";
    }
}
""")
    _write(root, "src/main/java/com/fab/mixin/WorldMixin.java", """package com.fab.mixin;

import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;

@Mixin(ServerWorld.class)
public class WorldMixin {
    @Inject(method = "tick", at = @At("HEAD"))
    private void fab$onTick(Object ci) {
    }

    private void helper() {
    }
}
""")
    g = index.load(_scan(root, git=False))
    fw = {(g.label(n), g.file(n).rsplit("/", 1)[-1]): e for n, e in am.framework_entries(g).items()}
    assert fw[(".onInitialize()", "Boot.java")] == {
        "basis": "declared",
        "why": ['fabric.mod.json entrypoint "main": com.fab.Boot (src/main/resources/fabric.mod.json)']}
    assert fw[(".run()", "Gen.java")]["why"] == [
        'fabric.mod.json entrypoint "fabric-datagen": com.fab.Gen::run (src/main/resources/fabric.mod.json)']
    assert fw[(".fab$onTick()", "WorldMixin.java")]["why"] == ["mixin @Inject into ServerWorld.tick"]
    assert (".helper()", "WorldMixin.java") not in fw
    assert fw[(".setup()", "Boot.java")]["basis"] == "framework"   # an event bus listener
    # a method handed to forEach runs at once: a registers edge (impact, trace), but no entry point
    assert (".handle()", "Boot.java") not in fw
    assert {(g.label(u), g.label(v)) for u, v, _d in g.edges({"registers"})} == {
        (".onInitialize()", ".handle()"), (".onInitialize()", ".setup()")}
    sinks = am._sinks(g)
    save = next(n for n in sinks if g.label(n) == ".save()")
    assert [e["kind"] for e in sinks[save]] == ["file-write"]
    assert not any(g.label(n) == ".help()" for n in sinks)   # the call written in a string is no sink


# -- the rules of the pass, on small fixtures ------------------------------------------------------------

JAVA_NET = """package com.cb.net;

import com.cb.util.Bus;
import com.cb.util.Handler;

public class Net {
    private Handler handler;

    public void start() {
        Bus.listen(Net::tick);
        Net.tick(null);
    }

    public void direct() {
        Net.tick(null);
        Bus.listen(Net::tick);
    }

    public void anon() {
        Bus.listen(new Runnable() {
            public void run() {
                Bus.listen(this::tick);
            }

            void tick() {
            }
        });
    }

    public void inLambda() {
        Bus.later(() -> Bus.listen(this::tick));
    }

    public void typed() {
        Bus.listen(handler::onEvent);
    }

    public void notArgument() {
        Runnable r = Net::helper;
    }

    public String text() {
        String s = "Bus.listen(Net::helper)";
        // Bus.listen(Net::helper);
        return \"\"\"
            Bus.listen(Net::helper) is how a text block would write it
            \"\"\";
    }

    public void overloaded() {
        Bus.listen(Net::twice);
    }

    public void constructors() {
        Bus.listen(Net::new);
    }

    public static void tick(Object o) {
    }

    static void helper() {
    }

    static void twice() {
    }

    static void twice(int n) {
    }
}
"""
JAVA_BUS = """package com.cb.util;

public final class Bus {
    public static void listen(Object handler) {
    }

    public static void later(Runnable r) {
    }
}
"""
JAVA_HANDLER = """package com.cb.util;

public class Handler {
    public void onEvent(Object e) {
    }
}
"""
KOTLIN_SVC = """package com.cb.kt

import com.cb.util.Bus

class Svc {
    fun direct() {
        Bus.listen(this::onTick)
    }

    fun inObject() {
        Bus.listen(object : Runnable {
            override fun run() {
                Bus.listen(this::onTick)
            }

            fun onTick() {}
        })
    }

    fun inLambda(other: Any) {
        with(other) {
            Bus.listen(this::onTick)
        }
    }

    fun bare() {
        Bus.listen(::onTick)
    }

    fun onTick() {}

    companion object {
        fun setup() {
            Bus.listen(::onTick)
        }
    }
}

object Registry {
    fun init() {
        Bus.listen(::onLoad)
    }

    fun onLoad() {}
}
"""


@pytest.fixture(scope="module")
def cb(tmp_path_factory):
    root = tmp_path_factory.mktemp("jvmcb") / "cb"
    _write(root, "src/main/java/com/cb/net/Net.java", JAVA_NET)
    _write(root, "src/main/java/com/cb/util/Bus.java", JAVA_BUS)
    _write(root, "src/main/java/com/cb/util/Handler.java", JAVA_HANDLER)
    _write(root, "src/main/kotlin/com/cb/kt/Svc.kt", KOTLIN_SVC)
    _scan(root, git=False)
    return root


def test_a_method_reference_does_not_suppress_a_direct_call(cb):
    g = index.load(cb)
    for caller in ("start", "direct"):   # reference first, then the call, and the other way round
        u = next(n for n in g.symbols_in("src/main/java/com/cb/net/Net.java") if g.label(n) == f".{caller}()")
        rels = sorted(d["relation"] for v, d in g.out_edges(u) if g.label(v) == ".tick()")
        assert rels == ["calls", "registers"], (caller, rels)
    # the call pass alone is what it was without the references
    plain = index.load(cb, augment=False)
    assert all(d["relation"] == "calls" for _u, _v, d in index.java_call_edges(plain))


def test_this_is_the_named_class_only(cb):
    regs = _regs(index.load(cb))
    assert ("Net.start", "Net.tick") in regs and ("Net.direct", "Net.tick") in regs
    assert ("Net.inLambda", "Net.tick") in regs          # a Java lambda's `this` is the enclosing instance
    assert not any(u == "Net.anon" for u, _v in regs)   # `this` in an anonymous class is that class
    assert ("Svc.direct", "Svc.onTick") in regs and ("Svc.bare", "Svc.onTick") in regs
    assert not any(u in ("Svc.inObject", "Svc.inLambda") for u, _v in regs)  # object expression, lambda receiver
    assert not any(v == "Svc.onTick" and u.endswith("setup") for u, v in regs)  # a companion object
    assert ("Registry.init", "Registry.onLoad") in regs  # a named object: `this` is that object


def test_strings_comments_text_blocks_and_non_arguments_are_not_references(cb):
    regs = _regs(index.load(cb))
    assert ("Net.typed", "Handler.onEvent") in regs      # `var::m` follows the declared type
    assert not any(v == "Net.helper" for _u, v in regs)  # an assignment, a string, a comment, a text block
    assert not any(u == "Net.constructors" for u, _v in regs)   # `::new` is a constructor
    assert ("Net.overloaded", "Net.twice") in regs   # the graph has one node for the overloads of a name


def test_a_class_is_resolved_by_the_imports_or_no_edge(tmp_path):
    root = tmp_path / "res"
    for pkg in ("a", "b"):
        _write(root, f"src/main/java/com/{pkg}/Tick.java",
               f"package com.{pkg};\n\npublic class Tick {{\n    public static void run(Object o) {{\n    }}\n}}\n")
    _write(root, "src/main/java/com/c/Uses.java", """package com.c;

import com.b.Tick;

public class Uses {
    public void go() {
        Hub.on(Tick::run);
    }
}
""")
    _write(root, "src/main/java/com/c/Guess.java", """package com.c;

public class Guess {
    public void go() {
        Hub.on(Tick::run);
    }
}
""")
    _write(root, "src/main/java/com/c/Hub.java",
           "package com.c;\n\npublic class Hub {\n    static void on(Object h) {\n    }\n}\n")
    g = index.load(_scan(root, git=False))
    edges = {(g.file(u).rsplit("/", 1)[-1], g.file(v)) for u, v, _d in g.edges({"registers"})}
    assert edges == {("Uses.java", "src/main/java/com/b/Tick.java")}   # Guess.java: no import, two Ticks


# -- claims ----------------------------------------------------------------------------------------------

def test_a_calls_claim_on_a_method_reference_is_graded_registers(glow):
    ref = entail.call_site(glow, TICK, 24, "tick")
    assert (ref.grade, ref.code) == ("none", "registers")
    assert "passes `RepairScheduler::tick` as a callback" in ref.reason
    call = entail.call_site(glow, "src/main/java/com/example/glowmod/GlowMod.java", 28, "RepairScheduler.register")
    assert call.code != "registers" and call.grade != "none"
    spec = {"target_label": "RepairScheduler.tick", "at": f"{TICK}:24", "free_text": True}
    text = "RepairScheduler.register calls RepairScheduler.tick"
    subjects = [TICK]
    src = evmod.source_evidence(glow, TICK, 24, commit=None)
    assert entail.assess("relation", glow, spec, src, text=text, subjects=subjects).code == "registers"
    # the registers edge as graph evidence is no call either
    g = index.load(glow)
    u, v, d = next((u, v, d) for u, v, d in g.edges({"registers"}) if g.label(v) == ".tick()")
    edge = evmod.graph_edge_evidence({"source": u, "target": v, **d}, graph_path=str(g.path), commit=None)
    gr = entail.assess("relation", glow, {**spec, "source": u, "target": v}, edge, text=text, subjects=subjects)
    assert (gr.grade, gr.code) == ("none", "registers")
    # a resolver (SCIP) that finds the referenced method definitively does not make it a call
    scip = evmod.source_evidence(glow, TICK, 24, commit=None, source_type="static_resolution",
                                 meta={"tool": "scip", "kind": "definitive", "token": "tick",
                                       "targets": [{"path": TICK, "line": 33}]})
    gr = entail.assess("relation", glow, spec, scip, text=text, subjects=subjects)
    assert (gr.grade, gr.code) == ("none", "registers") and gr.reason.startswith("scip: ")


def test_claim_add_on_a_method_reference_is_weaker_than_a_call(glow, tmp_path, capsys):
    from verinoda import cli

    repo = tmp_path / "glow"
    shutil.copytree(glow, repo)
    args = ["claim", "add", "RepairScheduler.register calls RepairScheduler.tick", "--kind", "relation",
            "--source", f"{TICK}:24", "--repo", str(repo), "--json"]
    assert cli.main(args) == 0
    import json

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "weak_inference"
    assert any("passes `RepairScheduler::tick` as a callback" in u for u in out["uncertainties"])
    assert out["qualifying"] and out["supporting"][0]["grade"] == "none"
    assert cli.main(["claim", "add", "GlowMod.onInitialize calls RepairScheduler.register", "--kind", "relation",
                     "--source", "src/main/java/com/example/glowmod/GlowMod.java:28", "--repo", str(repo),
                     "--json"]) == 0
    real = json.loads(capsys.readouterr().out)
    assert real["status"] == "strong_inference" and not real["uncertainties"]


# -- review fixes (2026-09-26) ------------------------------------------------------------------------------

JAVA_OUTER = """package com.rv;

public class Outer {
    public void anonWithLambdaArg(Bus bus) {
        bus.register(new Handler(() -> { return 1; }) {
            void on() { bus.register(this::helper); }
        });
    }

    public void anonWithArrayArg(Bus bus) {
        bus.register(new Handler(new int[]{1, 2}) {
            void on() { bus.register(this::helper); }
        });
    }

    public void named(Bus bus) {
        bus.register(this::helper);
    }

    // a text block starts with \"\"\" - this is a comment, not code
    public void afterComment(Bus bus) {
        bus.register(Outer::helper);
    }

    static void helper() {
    }
}
"""


def test_review_fixes_anonymous_classes_with_braces_in_arguments_and_triple_quotes_in_a_comment(tmp_path):
    root = tmp_path / "rv"
    _write(root, "src/main/java/com/rv/Outer.java", JAVA_OUTER)
    _write(root, "src/main/java/com/rv/Bus.java",
           "package com.rv;\n\npublic class Bus {\n    void register(Object o) {\n    }\n}\n")
    _write(root, "src/main/java/com/rv/Handler.java",
           "package com.rv;\n\npublic class Handler {\n    Handler(Object o) {\n    }\n}\n")
    g = index.load(_scan(root, git=False))
    regs = _regs(g)
    assert ("Outer.named", "Outer.helper") in regs
    assert not any(u in ("Outer.anonWithLambdaArg", "Outer.anonWithArrayArg") for u, _v in regs), regs
    assert ("Outer.afterComment", "Outer.helper") in regs   # the comment's quotes did not blank the rest
    assert index.has_registers(g) and g.__dict__.get("_has_registers") is True


def test_an_event_bus_subscriber_is_not_a_mod_class(tmp_path):
    root = tmp_path / "mdk"
    _write(root, "src/main/java/com/mdk/ExampleMod.java", """package com.mdk;

@Mod("examplemod")
public class ExampleMod {
    public ExampleMod() {
    }

    @Mod.EventBusSubscriber(modid = "examplemod")
    public static class ClientModEvents {
    }
}
""")
    _write(root, "src/main/java/com/mdk/ForgeEvents.java", """package com.mdk;

@Mod.EventBusSubscriber
public class ForgeEvents {
}
""")
    g = index.load(_scan(root, git=False))
    eps = am.framework_entries(g)
    why = {g.label(n).strip(".()"): " ".join(v["why"]) for n, v in eps.items()}
    mod = [k for k, w in why.items() if "@Mod class" in w]
    assert mod and all(k in ("ExampleMod",) for k in mod), why
    assert "@Mod class" not in why.get("ForgeEvents", "") and "@Mod class" not in why.get("ClientModEvents", "")
