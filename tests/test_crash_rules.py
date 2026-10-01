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
    return crash_rules.diagnose([trace_log._unprefixed(x) for x in text.splitlines()]), crash_rules.suspects(traces)


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
