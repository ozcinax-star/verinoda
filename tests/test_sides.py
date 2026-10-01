"""Client and server separation: client-only code reachable from server code (the map's ``sides`` view)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import architecture_map as am  # noqa: E402
from verinoda import index, map_text, search_index, sides, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

MAIN = "src/main/java/com/ex"
CLIENT = "src/client/java/com/ex/client"

FILES = {
    f"{MAIN}/Mod.java": """package com.ex;

import net.fabricmc.api.ModInitializer;

public class Mod implements ModInitializer {
    @Override
    public void onInitialize() {
        Helper.setup();
    }
}
""",
    f"{MAIN}/Helper.java": """package com.ex;

import com.ex.client.Hud;
import net.minecraft.client.MinecraftClient;

public class Helper {
    public static void setup() {
        Hud.draw();
        Annot.x();
        Mixed.render();
        Mixed.serverOnly();
        ClientEvents.hook();
        // MinecraftClient.getInstance() in a comment is not a use
        other();
    }

    static void other() {
        MinecraftClient.getInstance();
    }

    static void never() {
        Hud.draw();
    }
}
""",
    f"{MAIN}/Annot.java": """package com.ex;

import net.fabricmc.api.EnvType;
import net.fabricmc.api.Environment;

@Environment(EnvType.CLIENT)
public class Annot {
    public static void x() {
    }
}
""",
    f"{MAIN}/Mixed.java": """package com.ex;

import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.api.distmarker.OnlyIn;

public class Mixed {
    public static void common() {
    }

    @OnlyIn(Dist.CLIENT)
    public static void render() {
    }

    @OnlyIn(Dist.DEDICATED_SERVER)
    public static void serverOnly() {
    }
}
""",
    f"{MAIN}/ClientEvents.java": """package com.ex;

import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.fml.common.Mod;

@Mod.EventBusSubscriber(modid = "ex", value = Dist.CLIENT)
public class ClientEvents {
    public static void hook() {
    }
}
""",
    # a client initializer outside the client source set: not a server entry point
    f"{MAIN}/MainClientInit.java": """package com.ex;

import com.ex.client.Hud;
import net.fabricmc.api.ClientModInitializer;

public class MainClientInit implements ClientModInitializer {
    @Override
    public void onInitializeClient() {
        Hud.draw();
    }
}
""",
    f"{CLIENT}/Hud.java": """package com.ex.client;

public class Hud {
    public static void draw() {
    }
}
""",
    f"{CLIENT}/ModClient.java": """package com.ex.client;

import net.fabricmc.api.ClientModInitializer;

public class ModClient implements ClientModInitializer {
    @Override
    public void onInitializeClient() {
        Hud.draw();
    }
}
""",
    "src/main/resources/fabric.mod.json":
        '{"entrypoints": {"main": ["com.ex.Mod"], "client": ["com.ex.client.ModClient"]}}\n',
}


def _project(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
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


@pytest.fixture(scope="module")
def mod(tmp_path_factory) -> Path:
    return _project(tmp_path_factory.mktemp("sides") / "mod", FILES)


@pytest.fixture(scope="module")
def view(mod) -> dict:
    return sides.sides(index.load(mod))


def _by_subject(v: dict) -> dict[str, dict]:
    return {c["subject"]: c for c in v["claims"]}


def test_a_call_into_the_client_source_set_is_a_path_with_every_hop(view):
    c = _by_subject(view)["Hud.draw"]
    assert c["kind"] == "client_symbol_reached" and c["status"] == "strong_inference"
    assert c["entry"] == {"symbol": "Mod.onInitialize", "at": f"{MAIN}/Mod.java:7", "basis": "declared"}
    assert [(h["from"], h["relation"], h["to"], h["at"]) for h in c["path"]] == [
        ("Mod.onInitialize", "calls", "Helper.setup", f"{MAIN}/Mod.java:8"),
        ("Helper.setup", "calls", "Hud.draw", f"{MAIN}/Helper.java:8")]
    assert c["at"] == f"{MAIN}/Helper.java:8"
    assert c["client_only"]["basis"] == "source_set" and c["client_only"]["at"] == f"{CLIENT}/Hud.java:1"
    assert c["evidence_at"] == [f"{MAIN}/Mod.java:7", f"{MAIN}/Mod.java:8", f"{MAIN}/Helper.java:8",
                                f"{CLIENT}/Hud.java:1"]
    assert "reaches client-only symbol `Hud.draw`" in c["claim"] and "client source set" in c["claim"]


def test_annotations_mark_a_class_and_its_members_or_one_method(view):
    got = _by_subject(view)
    # the class annotation covers the method declared in it; the annotation line is cited
    assert got["Annot.x"]["client_only"]["at"] == f"{MAIN}/Annot.java:6"
    assert "@Environment(EnvType.CLIENT) on `Annot`" in got["Annot.x"]["client_only"]["why"]
    assert got["Mixed.render"]["client_only"]["at"] == f"{MAIN}/Mixed.java:10"
    assert got["ClientEvents.hook"]["client_only"]["at"] == f"{MAIN}/ClientEvents.java:6"
    # the annotation is quoted as written, strings and closing parenthesis included
    assert got["ClientEvents.hook"]["client_only"]["why"].startswith(
        '@Mod.EventBusSubscriber(modid = "ex", value = Dist.CLIENT) on `ClientEvents`')
    # a server-only method and the other methods of a partly client class are not client-only
    assert "Mixed.serverOnly" not in got and "Mixed.common" not in got


def test_a_client_package_class_named_in_a_reached_method_is_a_path(view):
    c = _by_subject(view)["net.minecraft.client.MinecraftClient"]
    assert c["kind"] == "client_class_use"
    assert c["at"] == f"{MAIN}/Helper.java:18"   # the use, not the comment at line 13
    assert [h["to"] for h in c["path"]] == ["Helper.setup", "Helper.other", "net.minecraft.client.MinecraftClient"]
    assert c["client_only"]["at"] == f"{MAIN}/Helper.java:4"   # the import line


def test_client_entry_points_are_left_out_and_unreached_crossings_listed(view):
    s = view["summary"]
    # ModClient (client source set), MainClientInit (ClientModInitializer) and ClientEvents (a Dist.CLIENT
    # event subscriber)
    assert s["server_entry_points"] == 1 and s["client_entry_points_left_out"] == 3
    assert [e["symbol"] for e in view["searched"]["entry_points"]] == ["Mod.onInitialize"]
    crossings = {(c["from"], c["at"]) for c in view["unreached_crossings"]}
    assert crossings == {("Helper.never", f"{MAIN}/Helper.java:22"),
                         ("MainClientInit.onInitializeClient", f"{MAIN}/MainClientInit.java:9")}
    assert s["unreached_crossings"] == 2
    # code inside the client source set calling client code is no crossing
    assert not any(c["from"].startswith("ModClient") for c in view["unreached_crossings"])
    assert s["paths"] == len(view["claims"]) == 5


def test_an_inferred_hop_makes_the_path_weak(tmp_path):
    files = {k: v for k, v in FILES.items() if k.endswith(("Mod.java", "Annot.java", "fabric.mod.json"))}
    files[f"{MAIN}/Helper.java"] = """package com.ex;

public class Helper {
    public static void setup() {
        new Annot();
    }
}
"""
    v = sides.sides(index.load(_project(tmp_path / "weak", files)))
    c = _by_subject(v)["Annot"]
    assert c["path"][-1]["confidence"] == "INFERRED" and c["status"] == "weak_inference"
    assert "reaches client-only class `Annot`" in c["claim"]


HUD = {f"{CLIENT}/Hud.java": FILES[f"{CLIENT}/Hud.java"]}


def test_only_the_side_part_of_an_entry_reason_puts_it_on_the_client(tmp_path):
    for why in ['fabric.mod.json entrypoint "client": com.ex.Mod (src/main/resources/fabric.mod.json)',
                "implements ClientModInitializer: Fabric calls onInitializeClient() at start",
                "@SubscribeEvent handler of TickEvent.ClientTickEvent",
                "@SubscribeEvent handler of net.minecraftforge.client.event.RenderGuiEvent",
                "mixin @Inject into MinecraftClient.tick",
                "mixin @Inject into net.minecraft.client.gui.Screen.render",
                "callback registered with ClientTickEvents.END_CLIENT_TICK.register(...) at a/B.java:3",
                "callback handed to PayloadRegistrar.playToClient(...) at a/B.java:3"]:
        assert sides._client_entry(why), why
    for why in ['fabric.mod.json entrypoint "main": com.ex.clientsync.Mod (src/client-mod/fabric.mod.json)',
                "implements ModInitializer: Fabric calls onInitialize() at start",
                "@SubscribeEvent handler of TickEvent.ServerTickEvent",
                "mixin @Inject into ServerPlayNetworkHandler.onClientCommand",
                "mixin @Inject into ServerPlayNetworkHandler.onClientSettings",
                "callback registered with ServerTickEvents.END_SERVER_TICK.register(...) at client/B.java:3",
                "@Mod class: the mod loader constructs it"]:
        assert not sides._client_entry(why), why
    # a package named like the client and a server mixin into a handler of a client packet stay server roots
    files = {"src/main/java/com/ex/clientsync/Mod.java": """package com.ex.clientsync;

import net.fabricmc.api.ModInitializer;
import net.minecraft.client.MinecraftClient;

public class Mod implements ModInitializer {
    @Override
    public void onInitialize() {
        MinecraftClient.getInstance();
    }
}
""",
             f"{MAIN}/mixin/NetMixin.java": """package com.ex.mixin;

import com.ex.client.Hud;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.Inject;

@Mixin(ServerPlayNetworkHandler.class)
public class NetMixin {
    @Inject(method = "onClientStatus", at = @At("HEAD"))
    private void onStatus(CallbackInfo ci) {
        Hud.draw();
    }
}
""",
             "src/main/resources/fabric.mod.json": '{"entrypoints": {"main": ["com.ex.clientsync.Mod"]}}\n', **HUD}
    v = sides.sides(index.load(_project(tmp_path / "names", files)))
    assert v["summary"]["server_entry_points"] == 2 and v["summary"]["client_entry_points_left_out"] == 0
    got = _by_subject(v)
    assert got["net.minecraft.client.MinecraftClient"]["at"] == "src/main/java/com/ex/clientsync/Mod.java:9"
    assert got["Hud.draw"]["entry"]["symbol"] == "NetMixin.onStatus"


def test_a_client_mod_class_is_client_only_and_a_both_sides_subscriber_is_not(tmp_path):
    files = {f"{MAIN}/ExClient.java": """package com.ex;

import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.fml.common.Mod;

@Mod(value = "ex", dist = Dist.CLIENT)
public class ExClient {
    public ExClient() {
        Minecraft.getInstance();
    }
}
""",
             f"{MAIN}/Events.java": """package com.ex;

import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.fml.common.Mod;

@Mod.EventBusSubscriber(modid = "ex", value = {Dist.CLIENT, Dist.DEDICATED_SERVER})
public class Events {
    @SubscribeEvent
    public static void onTick(TickEvent.ServerTickEvent e) {
        Util.go();
    }
}
""",
             f"{MAIN}/Util.java": """package com.ex;

import com.ex.client.Hud;

public class Util {
    public static void go() {
        Hud.draw();
    }
}
""", **HUD}
    v = sides.sides(index.load(_project(tmp_path / "neo", files)))
    s = v["summary"]
    # ExClient and its constructor (dist = Dist.CLIENT); Hud and Hud.draw (source set); not Events
    assert s["client_only_by_basis"] == {"annotation": 2, "source_set": 2}
    assert s["client_entry_points_left_out"] == 1   # the client @Mod constructor
    assert {e["symbol"] for e in v["searched"]["entry_points"]} == {"Events", "Events.onTick"}
    assert [c["subject"] for c in v["claims"]] == ["Hud.draw"]
    assert [h["to"] for h in v["claims"][0]["path"]] == ["Util.go", "Hud.draw"]


def test_a_constructor_is_walked_when_its_class_is_instantiated(tmp_path):
    files = {k: v for k, v in FILES.items() if k.endswith(("Mod.java", "fabric.mod.json"))}
    files[f"{MAIN}/Mod.java"] = FILES[f"{MAIN}/Mod.java"].replace("Helper.setup();", "new Thing();")
    files[f"{MAIN}/Thing.java"] = """package com.ex;

import net.minecraft.client.MinecraftClient;

public class Thing {
    public Thing() {
        MinecraftClient.getInstance();
    }
}
"""
    v = sides.sides(index.load(_project(tmp_path / "ctor", files)))
    c = _by_subject(v)["net.minecraft.client.MinecraftClient"]
    assert c["at"] == f"{MAIN}/Thing.java:7" and c["status"] == "weak_inference"
    assert [(h["from"], h["relation"], h["to"], h["at"]) for h in c["path"]][0] == (
        "Mod.onInitialize", "instantiates", "Thing.Thing", f"{MAIN}/Mod.java:8")


def test_a_kotlin_annotation_belongs_to_the_member_below_it_only(tmp_path):
    files = {f"{MAIN}/Mod.kt": """package com.ex

import net.fabricmc.api.ModInitializer

class Mod : ModInitializer {
    override fun onInitialize() {
        Util.tick()
        Util.render()
    }
}
""",
             f"{MAIN}/Util.kt": """package com.ex

import net.fabricmc.api.EnvType
import net.fabricmc.api.Environment

object Util {
    @Environment(EnvType.CLIENT)
    fun render() = 1
    fun tick() { println("server tick") }
}
""",
             "src/main/resources/fabric.mod.json": FILES["src/main/resources/fabric.mod.json"]}
    v = sides.sides(index.load(_project(tmp_path / "kt", files)))
    got = _by_subject(v)
    assert "Util.tick" not in got
    if "Util.render" in got:   # when the extractor resolved the call
        assert got["Util.render"]["client_only"]["at"] == f"{MAIN}/Util.kt:7"
    assert v["summary"]["client_only_symbols"] == 1


def test_no_server_entry_point_is_unknown_and_no_jvm_code_is_said(tmp_path):
    files = {f"{CLIENT}/Hud.java": FILES[f"{CLIENT}/Hud.java"],
             f"{MAIN}/Plain.java": "package com.ex;\n\npublic class Plain {\n    void f() {\n    }\n}\n"}
    v = sides.sides(index.load(_project(tmp_path / "noentry", files)))
    assert v["claims"] == [] and "no server-side entry point" in v["unknown"]
    assert "unknown:" in map_text.render({"sides": v}, 40)
    py = sides.sides(index.load(_project(tmp_path / "py", {"a.py": "def f():\n    return 1\n"})))
    assert py["claims"] == [] and py["summary"]["jvm_files"] == 0 and "no Java or Kotlin" in py["note"]


def test_text_rendering_and_view_registration(view):
    text = map_text.render({"sides": view}, 40)
    assert "== sides ==" in text and "limit:" in text
    assert "5 paths from server code to client-only code" in text
    assert "Mod.onInitialize -> Helper.setup -> Hud.draw" in text
    assert "no server entry point reaches" in text and "Helper.never calls Hud.draw" in text
    assert "sides" in am.VIEWS and "sides" not in am.DEFAULT_VIEWS   # asked for by name


def test_cli_map_view_sides_json(mod):
    out = subprocess.run([sys.executable, "-m", "verinoda", "map", str(mod), "--view", "sides", "--json"],
                         capture_output=True, text=True, encoding="utf-8", cwd=mod)
    assert out.returncode == 0, out.stderr
    v = json.loads(out.stdout)["sides"]
    assert "Hud.draw" in {c["subject"] for c in v["claims"]}
