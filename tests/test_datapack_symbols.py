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
""",
    "src/main/java/com/example/arena/Arena.java": """package com.example.arena;

public final class Arena {
    static final String STATE = "a_state";
    static final String BLUE = "blue";

    static void setUp(Scoreboard sb, String kind) {
        sb.addObjective(STATE, ObjectiveCriteria.DUMMY, null, null, false, null);
        sb.addObjective("a_" + kind, ObjectiveCriteria.DUMMY, null, null, false, null);
        sb.addPlayerTeam(BLUE);
        sb.getPlayerTeam("yellow");
    }

    static void run(Server server) {
        server.runCommand("team join purple @s");
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
