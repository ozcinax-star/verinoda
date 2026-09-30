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
    assert c["entry"] == {"symbol": "Mod.onInitialize", "at": f"{MAIN}/Mod.java:6", "basis": "declared"}
    assert [(h["from"], h["relation"], h["to"], h["at"]) for h in c["path"]] == [
        ("Mod.onInitialize", "calls", "Helper.setup", f"{MAIN}/Mod.java:8"),
        ("Helper.setup", "calls", "Hud.draw", f"{MAIN}/Helper.java:8")]
    assert c["at"] == f"{MAIN}/Helper.java:8"
    assert c["client_only"]["basis"] == "source_set" and c["client_only"]["at"] == f"{CLIENT}/Hud.java:1"
    assert c["evidence_at"] == [f"{MAIN}/Mod.java:6", f"{MAIN}/Mod.java:8", f"{MAIN}/Helper.java:8",
                                f"{CLIENT}/Hud.java:1"]
    assert "reaches client-only symbol `Hud.draw`" in c["claim"] and "client source set" in c["claim"]


def test_annotations_mark_a_class_and_its_members_or_one_method(view):
    got = _by_subject(view)
    # the class annotation covers the method declared in it; the annotation line is cited
    assert got["Annot.x"]["client_only"]["at"] == f"{MAIN}/Annot.java:6"
    assert "@Environment(EnvType.CLIENT) on `Annot`" in got["Annot.x"]["client_only"]["why"]
    assert got["Mixed.render"]["client_only"]["at"] == f"{MAIN}/Mixed.java:10"
    assert got["ClientEvents.hook"]["client_only"]["at"] == f"{MAIN}/ClientEvents.java:6"
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
