"""Java calls into a datapack's functions (D69): command strings, the function manager's lookup of an identifier,
and the project's own helpers around it, recognised by their body."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinoda import cli, datapack, datapack_java, setup

DP = "src/main/resources/data/demo/function"
J = "src/main/java/com/example/demo"

FILES = {
    f"{DP}/alpha.mcfunction": "say alpha\n",
    f"{DP}/beta.mcfunction": "execute as @a run function demo:alpha\n",
    f"{DP}/gamma.mcfunction": "say gamma\n",
    f"{DP}/delta.mcfunction": "say delta\n",
    f"{DP}/give_sword.mcfunction": "give @s iron_sword\n",
    f"{DP}/give_shield.mcfunction": "give @s shield\n",
    # the helper: a String parameter reaches the function manager's lookup with a constant namespace
    f"{J}/ExampleMod.java": """package com.example.demo;

public final class ExampleMod {
    public static final String NS = "demo";

    public static void runFunction(ServerPlayer p, String name) {
        MinecraftServer server = p.level().getServer();
        server.getFunctions().get(Identifier.fromNamespaceAndPath(NS, name))
                .ifPresent(fn -> server.getFunctions().execute(fn, p.createCommandSourceStack()));
    }

    static Identifier dimension(String path) {
        return Identifier.fromNamespaceAndPath("demo", path);   // not a function
    }
}
""",
    # a private helper of the same name with three parameters, its id held in a local
    f"{J}/Spells.java": """package com.example.demo;

final class Spells {
    private static void runFunction(ServerPlayer p, Vec3 at, String name) {
        Identifier id = Identifier.fromNamespaceAndPath("demo", name);
        MinecraftServer server = p.level().getServer();
        server.getFunctions().get(id).ifPresent(fn -> server.getFunctions().execute(fn, source(p, at)));
    }

    static void cast(ServerPlayer p, Vec3 at) {
        runFunction(p, at, "beta");      // MARK spells-beta
        ExampleMod.runFunction(p, "alpha");      // MARK spells-alpha
    }
}
""",
    f"{J}/Weapon.java": """package com.example.demo;

final class Weapon {
    static void fire(ServerPlayer p, ItemStack it, Registry<Thing> reg) {
        ExampleMod.runFunction(p, "alpha");      // MARK weapon-alpha
        it.set(DataComponents.ITEM_MODEL, Identifier.fromNamespaceAndPath("demo", "lantern"));      // MARK model
        var thing = reg.get(Identifier.fromNamespaceAndPath("demo", "gamma"));      // MARK registry
    }
}
""",
    f"{J}/Stage.java": """package com.example.demo;

final class Stage {
    // function demo:gamma  (a comment, not a call)
    static void close(MinecraftServer server, ServerPlayer p) {
        server.getCommands().performPrefixedCommand(server.createCommandSourceStack(),
                "function demo:gamma");      // MARK stage-plain
        server.runCommand("execute as " + p.getName() + " at " + p.getName()
                + " run function demo:gamma");      // MARK stage-concat
        String note = "the /execute ... run function demo:gamma retry";      // MARK prose
        LOGGER.warn("datapack function demo:{} does not exist", note);      // MARK log
        p.sendSystemMessage(Component.literal("Cleanup: /function demo:gamma"));      // MARK chat
        server.runCommand("schedule function demo:delta 20t");      // MARK schedule
        server.runCommand("function demo:missing_one");      // MARK missing
    }
}
""",
    f"{J}/Boss.java": """package com.example.demo;

final class Boss {
    private static void escape(MinecraftServer server) {
        server.getFunctions().get(Identifier.fromNamespaceAndPath("demo", "delta"))      // MARK boss-delta
                .ifPresent(fn -> server.getFunctions().execute(fn, server.createCommandSourceStack()));
    }

    static void give(ServerPlayer p, ItemStack item) {
        String kind = item.kind().toLowerCase(Locale.ROOT);
        ExampleMod.runFunction(p, "give_" + kind);      // MARK give-dynamic
        var fn = p.getServer().getFunctions()
                .get(Identifier.fromNamespaceAndPath("demo", "give_" + kind));      // MARK check-dynamic
    }

    // its parameter, after a constant prefix, is the name: a helper for demo:give_<argument>
    static void giveKit(ServerPlayer p, String kind) {
        ExampleMod.runFunction(p, "give_" + kind);
    }

    static void starter(ServerPlayer p) {
        giveKit(p, "shield");      // MARK kit-shield
    }
}
""",
    # a command-context helper: "ns:" + the argument unless it already names a namespace
    f"{J}/Commands.java": """package com.example.demo;

final class Commands {
    static int run(CommandContext<CommandSourceStack> ctx, String fnArg) {
        String fn = fnArg.contains(":") ? fnArg : "demo:" + fnArg;
        ServerFunctionManager functions = ctx.getSource().getServer().getFunctions();
        Identifier id = Identifier.parse(fn);
        return functions.get(id).isPresent() ? 1 : 0;
    }

    static void register(Dispatcher d) {
        d.register(literal("beta").executes(ctx -> run(ctx, "beta")));      // MARK commands-beta
        d.register(literal("other").executes(ctx -> run(ctx, "othermod:thing")));      // MARK commands-other
    }
}
""",
    # a method handing its own parameter to a helper is a helper too
    f"{J}/Campaign.java": """package com.example.demo;

final class Campaign {
    private static void dispatch(ServerPlayer p, String name) {
        ExampleMod.runFunction(p, name);
    }

    static void firstJoin(ServerPlayer p) {
        dispatch(p, "delta");      // MARK campaign-delta
    }
}
""",
    # a reference tree: the original plugin the mod was ported from
    "original/src/main/java/com/example/Plugin.java": """package com.example;

final class Plugin {
    void onDeath(Player p) {
        Bukkit.dispatchCommand(Bukkit.getConsoleSender(), "function demo:gamma");      // MARK ref-gamma
    }
}
""",
}


def _line(rel: str, mark: str) -> int:
    return next(i for i, ln in enumerate(FILES[rel].splitlines(), 1) if f"MARK {mark}" in ln)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("dpjava") / "mod"
    _write(root, FILES)
    setup.add_reference(root, "original")
    return root


def _called_by(repo: Path, fid: str) -> list[dict]:
    res = datapack.lookup(repo, "function", fid)
    return [c for c in res.get("called_by") or [] if c.get("kind") == "java"]


def test_static_helper_is_recognised_by_its_body(repo):
    rows = _called_by(repo, "demo:alpha")
    at = {(r["at"], r["via"], r.get("helper"), r["caller"]) for r in rows}
    assert at == {(f"{J}/Weapon.java:{_line(f'{J}/Weapon.java', 'weapon-alpha')}", "helper", "ExampleMod.runFunction",
                   "Weapon.fire"),
                  (f"{J}/Spells.java:{_line(f'{J}/Spells.java', 'spells-alpha')}", "helper", "ExampleMod.runFunction",
                   "Spells.cast")}
    java = datapack.lookup(repo)["java"]
    helpers = {h["helper"]: h for h in java["helpers"]}
    assert helpers["ExampleMod.runFunction"]["source"] == "body"
    assert helpers["ExampleMod.runFunction"]["prefix"] == "demo:"       # the namespace from a constant


def test_same_named_helpers_are_told_apart_by_class_and_arity(repo):
    rows = _called_by(repo, "demo:beta")
    at = {(r["at"], r.get("helper")) for r in rows}
    assert (f"{J}/Spells.java:{_line(f'{J}/Spells.java', 'spells-beta')}", "Spells.runFunction") in at
    assert (f"{J}/Commands.java:{_line(f'{J}/Commands.java', 'commands-beta')}", "Commands.run") in at
    helpers = {h["helper"]: h for h in datapack.lookup(repo)["java"]["helpers"]}
    assert helpers["Spells.runFunction"]["arity"] == 3 and helpers["Spells.runFunction"]["arg"] == 2
    assert helpers["ExampleMod.runFunction"]["arity"] == 2
    # a whole id handed to the command-context helper is used as it is (and another mod's is not "missing")
    missing = {r["name"] for r in datapack.lookup(repo)["problems"]["missing_functions"]}
    assert "othermod:thing" not in missing and "demo:othermod:thing" not in missing


def test_command_strings_read_as_commands_only(repo):
    rows = _called_by(repo, "demo:gamma")
    stage = f"{J}/Stage.java"
    got = {(r["at"], r["via"], r["how"]) for r in rows if not r.get("tree")}
    assert got == {(f"{stage}:{_line(stage, 'stage-plain')}", "command-string", "function"),
                   (f"{stage}:{_line(stage, 'stage-concat')}", "command-string", "execute run")}
    # the registry lookup of the same id is not a call; prose, a log line, a chat hint and a comment are not either
    lines = {int(r["at"].rsplit(":", 1)[1]) for r in rows if r["at"].startswith(stage)}
    assert not lines & {_line(stage, m) for m in ("prose", "log", "chat")}
    assert not any(r["at"].startswith(f"{J}/Weapon.java") for r in rows)


def test_reference_tree_calls_are_labelled(repo):
    ref = [r for r in _called_by(repo, "demo:gamma") if r.get("tree")]
    rel = "original/src/main/java/com/example/Plugin.java"
    assert ref == [{"kind": "java", "via": "command-string", "target": "demo:gamma", "caller": "Plugin.onDeath",
                    "at": f"{rel}:{_line(rel, 'ref-gamma')}", "how": "function", "tree": "original",
                    "text": ref[0]["text"]}]
    out = datapack.render(datapack.lookup(repo, "function", "demo:gamma"))
    assert "[reference tree original]" in out


def test_identifier_counts_only_in_the_function_lookup(repo):
    rows = _called_by(repo, "demo:delta")
    got = {(r["at"], r["via"], r.get("helper")) for r in rows}
    boss, camp, stage = f"{J}/Boss.java", f"{J}/Campaign.java", f"{J}/Stage.java"
    assert got == {(f"{boss}:{_line(boss, 'boss-delta')}", "identifier", None),
                   (f"{camp}:{_line(camp, 'campaign-delta')}", "helper", "Campaign.dispatch"),
                   (f"{stage}:{_line(stage, 'schedule')}", "command-string", None)}
    java = datapack.index(repo)["java"]
    weapon = f"{J}/Weapon.java"
    assert not [c for c in java.calls if c.file == weapon and c.line in (_line(weapon, "model"),
                                                                           _line(weapon, "registry"))]
    assert not [c for c in java.calls if c.file == f"{J}/ExampleMod.java"]    # dimension("x") is not a lookup
    helpers = {h["helper"]: h for h in datapack.lookup(repo)["java"]["helpers"]}
    assert helpers["Campaign.dispatch"]["source"] == "forwards to ExampleMod.runFunction"


def test_names_built_at_run_time_are_reported_once(repo):
    res = datapack.lookup(repo)
    boss = f"{J}/Boss.java"
    dyn = {(d["at"], d["target"], d["via"]) for d in res["java"]["dynamic"]}
    assert dyn == {(f"{boss}:{_line(boss, 'give-dynamic')}", "demo:give_*", "helper"),
                   (f"{boss}:{_line(boss, 'check-dynamic')}", "demo:give_*", "identifier")}
    text = datapack.render(res)
    assert f"dynamic: {boss}:{_line(boss, 'give-dynamic')} builds demo:give_*" in text
    one = datapack.lookup(repo, "function", "demo:give_sword")
    assert len(one["dynamic"]) == 2 and not _called_by(repo, "demo:give_sword")
    assert "may be this one" in datapack.render(one)
    assert datapack.lookup(repo, "function", "demo:alpha")["dynamic"] == []
    # a parameter after a constant prefix makes a helper; its constant argument binds
    kit = _called_by(repo, "demo:give_shield")
    assert [(r["at"], r["helper"]) for r in kit] == [(f"{boss}:{_line(boss, 'kit-shield')}", "Boss.giveKit")]
    helpers = {h["helper"]: h for h in res["java"]["helpers"]}
    assert helpers["Boss.giveKit"]["prefix"] == "demo:give_"


def test_java_call_to_a_missing_function_is_a_problem(repo, capsys):
    res = datapack.lookup(repo)
    stage = f"{J}/Stage.java"
    row = next(r for r in res["problems"]["missing_functions"] if r["name"] == "demo:missing_one")
    assert row["called_at"] == f"{stage}:{_line(stage, 'missing')}" and row["how"] == "java command-string"
    assert cli.main(["datapack", "function", "demo:missing_one", "--repo", str(repo)]) == 2
    out = capsys.readouterr().out
    assert "not found" in out and f"called by Java Stage.close at {stage}:{_line(stage, 'missing')}" in out


def test_text_view_and_json_records(repo, capsys):
    assert cli.main(["datapack", "function", "demo:alpha", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    weapon = f"{J}/Weapon.java"
    assert f"called by Java Weapon.fire at {weapon}:{_line(weapon, 'weapon-alpha')} (helper ExampleMod.runFunction)" \
        in out
    assert "called by demo:beta at" in out                                  # the mcfunction caller stays
    assert cli.main(["datapack", "function", "demo:gamma", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "no call in or out found" not in out                              # Java runs it
    assert cli.main(["datapack", "function", "demo:alpha", "--repo", str(repo), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    kinds = {c["kind"] for c in res["called_by"]}
    assert kinds == {"java", "mcfunction"}
    for c in res["called_by"]:
        if c["kind"] == "java":
            assert c["via"] in datapack_java.VIA and c["helper"] == "ExampleMod.runFunction"


def test_configured_helper_when_the_body_is_beyond_reading(tmp_path):
    root = tmp_path / "mod"
    _write(root, {f"{DP}/alpha.mcfunction": "say alpha\n",
                  f"{J}/Opaque.java": """package com.example.demo;

final class Opaque {
    static void trigger(Object who, String what) {
        Scheduler.later(() -> Bridge.run(who, what.trim()));
    }

    static void use(Object p) {
        trigger(p, "alpha");
    }
}
"""})
    assert not _called_by(root, "demo:alpha")
    (root / ".verinoda").mkdir()
    (root / ".verinoda" / "config.json").write_text(
        json.dumps({"datapack": {"function_helpers": ["com.example.demo.Opaque.trigger:1:demo"]}}), encoding="utf-8")
    rows = _called_by(root, "demo:alpha")
    assert [(r["via"], r["helper"], r["caller"]) for r in rows] == [("helper", "Opaque.trigger", "Opaque.use")]


@pytest.mark.parametrize("code, target, via, how", [
    ('server.runCommand("return run function demo:a");', "demo:a", "command-string", "execute run"),
    ('run("function #demo:tick_all");', "#demo:tick_all", "command-string", "function"),
    ('run("schedule function demo:a 2s replace");', "demo:a", "command-string", "schedule"),
    ('run("function demo:a {amount:3}");', "demo:a", "command-string", "function"),
    ('run("""\n    say hi\n    function demo:a\n    """);', "demo:a", "command-string", "function"),
    ('server.getCommandFunctionManager().getFunction(new Identifier("demo", "a"));', "demo:a", "identifier",
     "lookup"),
    ('server.getFunctions().get(ResourceLocation.parse("demo:a"));', "demo:a", "identifier", "lookup"),
])
def test_forms(tmp_path, code, target, via, how):
    text = "class T {\n    void m(MinecraftServer server) {\n        " + code + "\n    }\n}\n"
    res = datapack_java.scan(tmp_path, {"T.java": text})
    assert [(c.target, c.via, c.how, c.caller, c.dynamic) for c in res.calls] == [(target, via, how, "T.m", False)]


def test_helper_that_runs_a_command_string(tmp_path):
    text = """class Cmd {
    static void runNamed(MinecraftServer s, String name) {
        s.getCommands().performPrefixedCommand(s.createCommandSourceStack(), "function demo:" + name);
    }

    static void runWhole(MinecraftServer s, String id) {
        s.getCommands().performPrefixedCommand(s.createCommandSourceStack(), "execute as @a run function " + id);
    }

    static void use(MinecraftServer s) {
        runNamed(s, "alpha");
        runWhole(s, "demo:beta");
    }
}
"""
    res = datapack_java.scan(tmp_path, {"Cmd.java": text})
    assert [(c.target, c.via, c.helper, c.line, c.dynamic) for c in res.calls] == [
        ("demo:alpha", "helper", "Cmd.runNamed", 11, False), ("demo:beta", "helper", "Cmd.runWhole", 12, False)]


def test_helper_that_normalises_its_parameter_and_an_unreadable_lookup(tmp_path):
    text = """class H {
    static void run(MinecraftServer s, String name) {
        s.getFunctions().get(Identifier.fromNamespaceAndPath("demo", name.toLowerCase(Locale.ROOT)));
    }

    static void use(MinecraftServer s, Identifier fromConfig) {
        run(s, "alpha");
        s.getFunctions().get(ids.pick());
    }
}
"""
    res = datapack_java.scan(tmp_path, {"H.java": text})
    assert [(c.target, c.helper, c.line) for c in res.calls] == [("demo:alpha", "H.run", 7)]
    assert res.unresolved == ["H.java:8"]


@pytest.mark.parametrize("code", [
    'run("functions demo:a");',                                  # not the command
    'run("Type /function demo:a to clean up");',                 # prose
    'LOGGER.info("function demo:a failed");',                    # text after the id
    'map.get(Identifier.fromNamespaceAndPath("demo", "a"));',    # not the function manager
    'String s = "function demo:" ;',                            # no name
])
def test_not_calls(tmp_path, code):
    text = "class T {\n    void m(MinecraftServer server) {\n        " + code + "\n    }\n}\n"
    assert datapack_java.scan(tmp_path, {"T.java": text}).calls == []
