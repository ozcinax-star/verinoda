"""Known crash patterns and suspect mods of a log, for `verinoda trace-log` (verinoda/crash_rules.py).

The fixtures under tests/fixtures/crash are written by hand in the shape of real Fabric / Forge logs and crash
reports (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from verinoda import crash_rules, trace_log

CRASH = Path(__file__).parent / "fixtures" / "crash"


def _run(name: str) -> tuple[list[dict], list[dict]]:
    text = (CRASH / name).read_text(encoding="utf-8")
    traces, _results = trace_log.parse(text)
    return crash_rules.diagnose(text.splitlines()), crash_rules.suspects(traces)


@pytest.mark.parametrize("name, rule, line, found", [
    ("forge_out_of_memory.txt", "out_of_memory", 7, {"kind": "Java heap space"}),
    ("fabric_watchdog.txt", "watchdog", 5, {"seconds": "60.00"}),
    ("fabric_missing_dependency.log", "missing_dependency", 7, {"mod_id": "guardmod", "needs": "fabric-api"}),
    ("fabric_mixin_apply.log", "mixin_apply", 7, {"mod_id": "guardmod", "mixin": "guardmod.mixins.json:LevelMixin",
                                                  "target": "net.minecraft.class_1937"}),
    ("wrong_java.log", "wrong_java", 6, {"needs_java": 21, "runs_java": 17}),
])
def test_each_rule_names_its_first_line_and_what_it_found(name, rule, line, found):
    rules, _sus = _run(name)
    assert [d["rule"] for d in rules] == [rule]
    d = rules[0]
    assert d["log_line"] == line and d["means"] and d["evidence"]
    assert found.items() <= d["found"].items()


def test_a_bare_header_takes_what_a_later_line_found():
    rules, _sus = _run("fabric_watchdog.txt")
    assert rules[0]["evidence"] == "Description: Watching Server" and rules[0]["found_at"] == 7
    assert rules[0]["hits"] == 2


def test_suspects_by_jar_scored_from_positions_and_the_head_section_not_counted_twice():
    _rules, sus = _run("forge_out_of_memory.txt")
    assert [(s["suspect"], s["score"]) for s in sus] == [("bigstorage-2.4.1.jar", 0.78), ("tinyhud-1.0.3.jar", 0.1)]
    top = sus[0]
    assert top["frames"] == 3 and top["positions"] == [2, 3, 4] and top["top_frames"] == 0
    assert top["status"] == "strong_inference" and top["named_by"] == "the jar the frame names"
    assert "StorageNetwork.collectStacks" in top["first"]


def test_a_mixin_handler_and_the_mods_package_are_one_suspect():
    _rules, sus = _run("fabric_watchdog.txt")
    assert len(sus) == 1
    s = sus[0]
    assert s["suspect"] == "dev.lagfix.chunk (mod lagfix)" and s["score"] == 1.83
    assert s["positions"] == [0, 1, 2] and s["top_frames"] == 1 and "Mixin handler" in s["named_by"]


def test_frames_of_the_game_loader_and_jdk_are_never_suspects():
    for name in ("fabric_mixin_apply.log", "fabric_missing_dependency.log"):
        assert _run(name)[1] == []


def test_caused_by_restarts_the_position():
    log = ("java.lang.RuntimeException: wrapped\n\tat net.minecraft.A.a(A.java:1)\n\tat net.minecraft.B.b(B.java:2)\n"
           "Caused by: java.lang.NullPointerException\n\tat org.foo.bar.Baz.qux(Baz.java:9)\n\t... 2 more\n")
    traces, _ = trace_log.parse(log)
    assert [f.pos for f in traces[0].frames] == [0, 1, 0]
    assert [(s["suspect"], s["score"], s["top_frames"]) for s in crash_rules.suspects(traces)] == [
        ("org.foo.bar", 1.0, 1)]


def test_a_missing_class_and_a_clean_log():
    assert [d["rule"] for d in crash_rules.diagnose([
        "java.lang.NoClassDefFoundError: net/minecraft/client/gui/screens/Screen"])] == ["missing_class"]
    assert crash_rules.diagnose(["[10:00:00] [Server thread/INFO]: Done (3.2s)!"]) == []


def _sus(log: str) -> list[tuple]:
    return [(s["suspect"], s["score"]) for s in crash_rules.suspects(trace_log.parse(log)[0])]


def test_a_crash_report_sections_stacktrace_is_not_counted_again():
    """Every category section (`-- Block entity being ticked --`, `-- Affected level --`) prints a `Stacktrace:` that
    is a run of the main trace; only the main trace counts."""
    main = ("java.lang.NullPointerException: boom\n"
            "\tat com.bigstorage.a.B.c(B.java:1)\n"
            "\tat net.minecraft.world.level.chunk.LevelChunk.tick(LevelChunk.java:2)\n"
            "\tat com.lagopt.hook.TickHook.wrap(TickHook.java:3)\n"
            "\tat net.minecraft.world.level.Level.tickBlockEntities(Level.java:4)\n"
            "\tat net.minecraft.server.MinecraftServer.tick(MinecraftServer.java:5)\n")
    report = (main + "\n-- Head --\nThread: Server thread\nStacktrace:\n\tat com.bigstorage.a.B.c(B.java:1)\n"
              "\n-- Block entity being ticked --\nDetails:\n\tName: x\nStacktrace:\n"
              + "".join(main.splitlines(keepends=True)[2:]))
    assert _sus(report) == _sus(main) == [("com.bigstorage.a", 1.0), ("com.lagopt.hook", 0.33)]
    sus = crash_rules.suspects(trace_log.parse(report)[0])
    assert sus[1]["traces"] == 1 and sus[1]["positions"] == [2]


def test_another_exception_at_the_same_place_is_counted():
    log = ("java.lang.IllegalStateException: A\n\tat com.a.b.C.d(C.java:1)\n\tat com.a.b.C.e(C.java:2)\n"
           "[10:00:00] [Server thread/INFO]: later\n"
           "java.lang.NullPointerException: other crash\n\tat com.a.b.C.d(C.java:1)\n")
    assert _sus(log) == [("com.a.b", 2.5)]


def test_a_trace_printed_twice_counts_once_and_many_traces_stay_fast():
    one = "java.lang.IllegalStateException: A\n\tat com.a.b.C.d(C.java:1)\n"
    assert _sus(one + one) == [("com.a.b", 1.0)]
    many = "".join(f"java.lang.IllegalStateException: {n}\n\tat com.a.b.C.d(C.java:{n})\nStacktrace:\n"
                   f"\tat com.a.b.C.d(C.java:{n})\n" for n in range(2000))
    import time
    t0 = time.perf_counter()
    assert _sus(many) == [("com.a.b", 2000.0)]
    assert time.perf_counter() - t0 < 2


def test_an_optional_class_warning_is_not_a_missing_class():
    rules = crash_rules.diagnose([
        "[10:00:01] [main/WARN] (mixin) Error loading class: dev/emi/emi/api/EmiPlugin "
        "(java.lang.ClassNotFoundException: dev/emi/emi/api/EmiPlugin)",
        "[10:00:02] [main/INFO] (Minecraft) Loaded",
        "[10:00:03] [main/ERROR] (Minecraft) Crashed",
        "java.lang.OutOfMemoryError: Java heap space"])
    assert [(d["rule"], d["log_line"]) for d in rules] == [("out_of_memory", 4)]


@pytest.mark.parametrize("line, found", [
    ("\t - Mod 'Guard Mod' (guardmod) 1.2.0 requires any version between 1.0 (inclusive) and 2.0 (exclusive) of mod "
     "'Cloth Config' (cloth-config), which is missing!", {"mod_id": "guardmod", "needs": "Cloth Config",
                                                         "needs_id": "cloth-config"}),
    ("\tMod ID: 'architectury', Requested by: 'rei', Expected range: '[9.1.12,)', Actual version: '[MISSING]'",
     {"mod_id": "rei", "needs_id": "architectury"}),
])
def test_missing_dependency_wordings(line, found):
    (d,) = crash_rules.diagnose([line])
    assert d["rule"] == "missing_dependency" and found.items() <= d["found"].items()


def test_a_mixin_handler_of_a_mod_id_with_a_dash_is_a_frame():
    log = ("java.lang.NullPointerException: boom\n"
           "\tat net.minecraft.class_310.handler$zdo000$fabric-lifecycle-events-v1$onStopping(class_310.java:1200)\n"
           "\tat net.minecraft.class_310.handler$zdo000$sodium$onRender(class_310.java:1210)\n")
    assert _sus(log) == [("mod fabric-lifecycle-events-v1", 1.0), ("mod sodium", 0.5)]


def test_a_generic_mod_id_ties_no_package():
    log = ("java.lang.NullPointerException: boom\n\tat net.minecraft.A.handler$zdo000$core$tick(A.java:1)\n"
           "\tat com.alpha.core.Thing.run(Thing.java:2)\n\tat com.beta.core.Other.run(Other.java:3)\n")
    assert _sus(log) == [("mod core", 1.0), ("com.alpha.core", 0.5), ("com.beta.core", 0.33)]


def test_a_forge_module_names_the_mod_and_ties_its_jar_and_package():
    log = ("java.lang.NullPointerException: boom\n"
           "\tat TRANSFORMER/bigstorage@2.4.1/com.bigstorage.network.StorageNetwork.rebuild(StorageNetwork.java:211) "
           "~[bigstorage-2.4.1.jar%23188!/:2.4.1] {}\n"
           "\tat com.bigstorage.network.StorageNetwork.tick(StorageNetwork.java:20) ~[?:?] {}\n"
           "\tat com.bigstorage.block.Controller.tick(Controller.java:9) ~[bigstorage-2.4.1.jar%23188!/:2.4.1] {}\n")
    sus = crash_rules.suspects(trace_log.parse(log)[0])
    assert [(s["suspect"], s["score"]) for s in sus] == [
        ("bigstorage-2.4.1.jar (mod bigstorage, com.bigstorage.network)", 1.83)]
    assert "module" in sus[0]["named_by"] and sus[0]["positions"] == [0, 1, 2]


def test_obfuscated_game_classes_and_hidden_lambda_classes():
    log = ("java.lang.NullPointerException: x\n\tat dzv.a(SourceFile:123)\n\tat dzv.b(SourceFile:12)\n"
           "\tat com.example.mymod.Foo.run(Foo.java:4)\n"
           "\tat com.example.mymod.Foo$$Lambda$1234/0x0000000800c3b440.accept(Unknown Source)\n"
           "\tat net.minecraft.client.main.Main.main(SourceFile:1)\n")
    assert _sus(log) == [("com.example.mymod", 0.58)]


def test_other_wrong_java_wordings():
    (d,) = crash_rules.diagnose(["java.lang.IllegalArgumentException: Unsupported class file major version 65"])
    assert d["rule"] == "wrong_java" and d["found"]["needs_java"] == 21
    assert [d["rule"] for d in crash_rules.diagnose(
        ["Error: LinkageError occurred while loading main class net.fabricmc.loader.impl.launch.knot.KnotClient"])] == [
        "wrong_java"]


def test_found_is_capped_and_a_rule_hit_carries_its_status():
    (d,) = crash_rules.diagnose(["java.lang.OutOfMemoryError: " + "x" * 5000])
    assert len(d["found"]["kind"]) == 200 and d["status"] == "strong_inference"


def test_a_fabric_loader_line_with_no_colon_loses_its_prefix():
    (d,) = _run("fabric_missing_dependency.log")[0][:1]
    assert trace_log._unprefixed("[11:03:27] [main/ERROR] (FabricLoader) Incompatible mods found!") == \
        "Incompatible mods found!"
    assert d["evidence"].startswith("- Mod 'Guard Mod'")
