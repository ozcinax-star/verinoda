"""Objectives, teams and boss bars used but never declared, and the naming rules of `datapack.naming`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinoda import cli, datapack

DP = "src/main/resources/data/arena/function"

FILES = {
    f"{DP}/load.mcfunction": """scoreboard objectives add a_kills playerKillCount
scoreboard objectives add a_vote trigger
team add red
bossbar add arena:timer "Time"
$scoreboard objectives add $(name) dummy
""",
    f"{DP}/tick.mcfunction": """scoreboard players add @a a_kills 0
execute if score #game a_state matches 1 run scoreboard players add #game a_ticks 1
scoreboard objectives setdisplay sidebar a_board
trigger a_vote
team join red @a[team=!blue]
team modify green color green
bossbar set arena:timer value 10
execute store result bossbar timer_old max run scoreboard players get #game a_ticks
tag @s add arena_player
execute as @a[tag=arena_player] run function arena:load
tellraw @a {"text":"Click to trigger the door, or join the team list"}
say join the team list now
$team join red_$(suffix) @s
$bossbar set arena:bar_$(id) value 3
$scoreboard players set @s obj_$(k) 1
scoreboard players set @s a_durum 1
execute as @a run scoreboard players add @s a_cmd 1
scoreboard objectives remove a_kills
""",
    "src/main/java/com/example/arena/Arena.java": """package com.example.arena;

import net.minecraft.world.scores.Scoreboard;

public final class Arena {
    static final String STATE = "a_state";
    static final String BLUE = "blue";
    static final String CMD = "a_cmd";

    static void setUp(Scoreboard sb, String kind) {
        sb.addObjective(STATE, ObjectiveCriteria.DUMMY, null, null, false, null);
        sb.addObjective("a_" + kind, ObjectiveCriteria.DUMMY, null, null, false, null);
        sb.addPlayerTeam(BLUE);
        sb.getPlayerTeam("yellow");
    }

    static void run(Server server) {
        server.runCommand("team join purple @s");
    }

    static void ensure(Scoreboard sb, String name) {
        sb.addObjective(name, ObjectiveCriteria.DUMMY, null, null, false, null);
    }

    static void init(Scoreboard sb, Server server) {
        ensure(sb, "a_durum");
        server.runCommand("scoreboard objectives add " + CMD + " dummy");
        LOGGER.warn("Could not trigger reload for scoreboard state");
        LOGGER.info("Player failed to join team list refresh");
    }
}
""",
    "src/main/java/com/example/arena/YarnSide.java": """package com.example.arena;

import net.minecraft.scoreboard.Scoreboard;

final class YarnSide {
    static Object of(Scoreboard sb) {
        return sb.getPlayerTeam("Steve");
    }
}
""",
}


@pytest.fixture()
def repo(tmp_path) -> Path:
    root = tmp_path / "mod"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    return root


def _at(rel: str, needle: str) -> str:
    return f"{rel}:{next(i for i, t in enumerate(FILES[rel].splitlines(), 1) if needle in t)}"


def test_parse_symbols_reads_teams_boss_bars_and_macro_declarations():
    rows = datapack.parse_symbols(FILES[f"{DP}/load.mcfunction"])
    assert (3, "team", "red", "define") in rows and (4, "bossbar", "arena:timer", "define") in rows
    assert (5, "objective", "*", "define") in rows                     # a macro fills in the name
    rows = datapack.parse_symbols(FILES[f"{DP}/tick.mcfunction"])
    assert (5, "team", "red", "use") in rows and (5, "team", "blue", "check") in rows
    assert (6, "team", "green", "use") in rows
    assert (8, "bossbar", "minecraft:timer_old", "use") in rows          # an id without a namespace is minecraft's
    _c, _t, objs = datapack.parse_function(FILES[f"{DP}/tick.mcfunction"])
    assert (3, "a_board", "read") in objs and (4, "a_vote", "write") in objs


def test_undeclared_objectives_teams_and_boss_bars(repo):
    pr = datapack.problems(datapack.index(repo, java_calls=False))
    objs = {r["name"]: r for r in pr["objectives_used_never_declared"]}
    # a_kills and a_vote are added by the datapack, a_state by Java through its constant
    assert set(objs) == {"a_board", "a_ticks"}
    tick = f"{DP}/tick.mcfunction"
    assert objs["a_ticks"]["sites"][0] == _at(tick, "a_ticks 1") and objs["a_ticks"]["status"] == "strong_inference"
    assert objs["a_board"]["maybe_declared_by"] == [{"at": _at("src/main/java/com/example/arena/Arena.java",
                                                               '"a_" + kind'), "pattern": "a_*"}]
    # red by team add, blue by Java; purple is joined from a Java command string
    assert [r["name"] for r in pr["teams_used_never_declared"]] == ["green", "purple", "yellow"]
    assert [r["name"] for r in pr["bossbars_used_never_declared"]] == ["minecraft:timer_old"]
    assert pr["declared_by_macros"] == [{"kind": "objective", "at": _at(f"{DP}/load.mcfunction", "$(name)")}]


def test_naming_rules_are_off_by_default_and_read_from_the_config(repo):
    ix = datapack.index(repo, java_calls=False)
    assert "naming" not in datapack.problems(ix)
    (repo / ".verinoda").mkdir()
    (repo / ".verinoda" / "config.json").write_text(json.dumps({"datapack": {"naming": {
        "objectives": "a_[a-z]+", "team": "[a-z]{4,}", "tag": "(", "colour": "x"}}}), encoding="utf-8")
    rules, bad = datapack.naming_rules(repo)
    assert set(rules) == {"objective", "team"} and {b["kind"] for b in bad} == {"tag", "colour"}
    rows = datapack.naming_problems(ix, rules)
    assert [(r["kind"], r["name"], r["at"]) for r in rows] == [
        ("team", "red", _at(f"{DP}/load.mcfunction", "team add red"))]


def test_cli_summary_and_lookup(repo, capsys):
    (repo / ".verinoda").mkdir()
    (repo / ".verinoda" / "config.json").write_text(json.dumps({"datapack": {"naming": {"team": "[a-z]{4,}"}}}),
                                                    encoding="utf-8")
    assert cli.main(["datapack", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "objectives used but never declared (2):" in out and "teams used but never declared (3):" in out
    assert "boss bars used but never declared (1):" in out
    assert "names that break the naming rules (1):" in out and "team red" in out
    assert cli.main(["datapack", "team", "green", "--repo", str(repo), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["declared"] is False and res["teams"] == 5
    assert cli.main(["datapack", "bossbar", "timer_old", "--repo", str(repo)]) == 0
    assert "never declared" in capsys.readouterr().out
    assert cli.main(["datapack", "score", "a_kills", "--repo", str(repo)]) == 0
    assert "never declared" not in capsys.readouterr().out


def test_words_in_text_are_not_commands():
    # "trigger", "team" and "bossbar" in a tellraw, a say or a log message are English, not commands
    text = FILES[f"{DP}/tick.mcfunction"]
    _c, _t, objs = datapack.parse_function(text)
    assert not [o for o in objs if o[1] in ("the", "door", "reload", "help")]
    assert not [r for r in datapack.parse_symbols(text) if r[2] in ("list", "now")]
    _c, _t, objs = datapack.parse_function('LOGGER.warn("Could not trigger ability for player")')
    assert objs == []
    assert datapack.parse_function("execute as @a run trigger a_vote")[2] == [(1, "a_vote", "write")]
    assert datapack.parse_symbols("/team join red @s") == [(1, "team", "red", "use")]


def test_a_name_a_macro_fills_in_part_is_not_a_name():
    rows = datapack.parse_symbols("$team join red_$(suffix) @s\n$bossbar set ns:bar_$(id) value 3\n$team add r_$(x)")
    assert rows == [(3, "team", "*", "define")]
    assert datapack.parse_function("$scoreboard players set @s obj_$(k) 1")[2] == []
    assert datapack.parse_function("$function ns:do_$(x)") == ([], [], [])       # not a call to ns:do_
    assert datapack.parse_function("$tag @s add a_$(x)")[1] == [(1, "*", "add")]  # a tag a macro fills in


def test_objectives_remove_is_a_use_not_a_declaration():
    assert datapack.parse_function("scoreboard objectives remove a_x")[2] == [(1, "a_x", "remove")]


def test_declared_by_a_helper_a_concatenation_and_not_by_yarn_get_player_team(repo):
    ix = datapack.index(repo, java_calls=False)
    pr = datapack.problems(ix)
    names = {r["name"] for r in pr["objectives_used_never_declared"]}
    assert "a_durum" not in names and "a_cmd" not in names       # ensure(sb, "a_durum"); "... add " + CMD + " ..."
    arena = "src/main/java/com/example/arena/Arena.java"
    assert any(s.kind == "define" and s.at == _at(arena, "ensure(sb, \"a_durum\")") for s in ix["objectives"]["a_durum"])
    # the helper hands its own parameter on: no "*" lead on every name, and nothing built at run time but "a_" + kind
    assert all("maybe_declared_by" not in r or all(d["pattern"] != "*" for d in r["maybe_declared_by"])
               for r in pr["objectives_used_never_declared"])
    assert [d["pattern"] for d in pr["declared_dynamically"]] == ["a_*"]
    assert "Steve" not in ix["teams"]                             # Yarn's getPlayerTeam takes a player's name
    assert not {"red_", "ns:bar_", "arena:bar_", "obj_", "reload", "refresh", "list", "now"} & (
        set(ix["objectives"]) | set(ix["teams"]) | set(ix["bossbars"]))
    kills = {s.kind for s in ix["objectives"]["a_kills"]}
    assert {"define", "remove"} <= kills and "a_kills" not in names


def test_naming_rules_that_could_hang_or_are_misshapen_are_reported(repo):
    (repo / ".verinoda").mkdir()
    cfg = repo / ".verinoda" / "config.json"
    cfg.write_text(json.dumps({"datapack": {"naming": {"team": "(a|aa)*", "objectives": "[a-z_]+",
                                                       "objective": "x"}}}), encoding="utf-8")
    rules, bad = datapack.naming_rules(repo)
    assert set(rules) == {"objective"} and rules["objective"].pattern == "[a-z_]+"
    assert [(b["kind"], b["rule"]) for b in bad] == [("team", "(a|aa)*"), ("objective", "x")]
    assert "exponential" in bad[0]["why"] and "set twice" in bad[1]["why"]
    cfg.write_text(json.dumps({"datapack": {"naming": "^[a-z]+$"}}), encoding="utf-8")
    assert datapack.naming_rules(repo) == ({}, [{"kind": "naming", "rule": "^[a-z]+$",
                                                 "why": "datapack.naming is an object: {kind: regex}"}])


def test_score_lookup_carries_the_built_name_lead(repo, capsys):
    assert cli.main(["datapack", "score", "a_board", "--repo", str(repo), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["declared"] is False and [d["pattern"] for d in res["maybe_declared_by"]] == ["a_*"]
    assert cli.main(["datapack", "score", "a_board", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "never declared by name" in out and "declares a_*" in out


def _mini(tmp_path, mcfunction: str, java: str) -> Path:
    root = tmp_path / "mini"
    (root / "data/ns/function").mkdir(parents=True)
    (root / "data/ns/function/a.mcfunction").write_bytes(mcfunction.encode("utf-8"))
    (root / "A.java").write_bytes(java.encode("utf-8"))
    return root


def test_a_java_string_that_starts_with_a_command_word_is_a_command_only_when_the_rest_fits(tmp_path):
    java = """class A { void f() {
    LOGGER.info("trigger fired for player");
    LOGGER.debug("team list refreshed ok");
    LOGGER.info("bossbar set up done");
    s.run("team join red @s");
    s.run("execute as @a run trigger vote set 1");
    s.run("bossbar set ns:x value 3");
} }
"""
    ix = datapack.index(_mini(tmp_path, "say hi\n", java), java_files=["A.java"], java_calls=False)
    assert set(ix["objectives"]) == {"vote"} and set(ix["teams"]) == {"red"} and set(ix["bossbars"]) == {"ns:x"}
    assert [r["name"] for r in datapack.problems(ix)["scores_written_never_read"]] == ["vote"]


def test_declared_by_a_format_string_and_a_text_block(tmp_path):
    mcf = "".join(f"scoreboard players set @s {n} 1\nexecute if score @s {n} matches 1 run say x\n"
                  for n in ("kills", "k_f2", "k_blk", "k_dyn"))
    java = '''class A {
    static final String K = "kills";
    void f() {
        s.run(String.format("scoreboard objectives add %s dummy", K));
        s.run("scoreboard objectives add %s dummy".formatted("k_f2"));
        s.run(String.format("scoreboard objectives add k_%s dummy", kind));
        s.run("""
            scoreboard objectives add k_blk dummy
            """);
    }
}
'''
    ix = datapack.index(_mini(tmp_path, mcf, java), java_files=["A.java"], java_calls=False)
    pr = datapack.problems(ix)
    assert [r["name"] for r in pr["objectives_used_never_declared"]] == ["k_dyn"]
    assert [d["pattern"] for d in pr["objectives_used_never_declared"][0]["maybe_declared_by"]] == ["k_*"]
    assert {s.at for s in ix["objectives"]["k_blk"] if s.kind == "define"} == {"A.java:8"}
    assert "k_" not in ix["objectives"]


def test_a_click_event_runs_trigger():
    line = 'tellraw @a {"text":"[Vote]","clickEvent":{"action":"run_command","value":"/trigger vote set 1"}}'
    assert datapack.parse_function(line)[2] == [(1, "vote", "write")]
    assert datapack.parse_function("tellraw @a {text:'x',click_event:{action:'run_command',command:'/trigger v2'}}"
                                   )[2] == [(1, "v2", "write")]


def test_naming_rules_that_backtrack_without_nested_repeats_are_refused():
    assert "exponential" in datapack._risky_rule(r"(a?){26}a{26}")
    assert "polynomial" in datapack._risky_rule(r"\w*\w*\w*\w*\w*\w*!")
    assert "at most 64" in datapack._risky_rule("(a|ab)" * 7)
    for ok in ("[a-z]+_[a-z]+", "(obj|objective)_[a-z0-9]+", "^[a-z0-9_.]+$", "[a-z]{4,}"):
        assert datapack._risky_rule(ok) is None


def test_a_long_macro_line_and_many_helper_methods_stay_linear():
    import time

    start = time.perf_counter()
    datapack.parse_function("$say " + "a" * 40000)
    datapack.parse_symbols("$say " + "a" * 40000)
    assert datapack._unmacro("$team join a$(x)b$(y)c red_$(s) @s") == "team join $(m) $(m) @s"
    src = "class A {\n" + "".join(f'void m{i}(Scoreboard sb, String n{i}) {{ sb.addObjective("o{i}", x); }}\n'
                                  for i in range(1500)) + "}"
    assert datapack._declare_helpers({"A.java": src}) == {}
    assert time.perf_counter() - start < 5
    src = "class A {\n void mk(Scoreboard sb, String n) { sb.addObjective(n, x); }\n}"
    assert datapack._declare_helpers({"A.java": src}) == {"mk": ("objective", 1)}
