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


# -- review fixes: which method a call resolves to, overloads, tables, formats, lines -----------------------------

LOOKUP = 's.getFunctions().get(Identifier.fromNamespaceAndPath("demo", name));'
JM = "src/main/java"


def _calls(res) -> set[tuple]:
    return {(c.file, c.line, c.target, c.dynamic) for c in res.calls}


def _util(pkg: str, body: str) -> str:
    return (f"package {pkg};\n\npublic final class Util {{\n    public static void run(MinecraftServer s, String name) {{\n"
            f"        {body}\n    }}\n}}\n")


def _user(pkg: str, cls: str, imports: str, calls: str) -> str:
    return f"package {pkg};\n\n{imports}class {cls} {{\n    void m(MinecraftServer s) {{\n{calls}    }}\n}}\n"


def test_a_same_named_class_in_another_package_is_not_the_helper(tmp_path):
    files = {
        f"{DP}/alpha.mcfunction": "say alpha\n",
        f"{JM}/a/Util.java": _util("a", LOOKUP),
        f"{JM}/b/Util.java": _util("b", "System.out.println(name);"),
        f"{JM}/c/User.java": _user("c", "User", "import b.Util;\n\n", '        Util.run(s, "hello_world");\n'),
        f"{JM}/d/Other.java": _user("d", "Other", "import a.Util;\n\n", '        Util.run(s, "alpha");\n'),
        f"{JM}/e/Wild.java": _user("e", "Wild", "import a.*;\n\n", '        Util.run(s, "alpha");\n'),
        f"{JM}/f/Full.java": _user("f", "Full", "", '        a.Util.run(s, "alpha");\n'
                                                    '        b.Util.run(s, "hello_world");\n'),
        f"{JM}/a/Same.java": _user("a", "Same", "", '        Util.run(s, "alpha");\n'),
        f"{JM}/g/Blind.java": _user("g", "Blind", "", '        Util.run(s, "hello_world");\n'),
    }
    root = tmp_path / "mod"
    _write(root, files)
    res = datapack_java.scan(root, {k: v for k, v in files.items() if k.endswith(".java")})
    assert _calls(res) == {(f"{JM}/d/Other.java", 7, "demo:alpha", False), (f"{JM}/e/Wild.java", 7, "demo:alpha", False),
                           (f"{JM}/f/Full.java", 5, "demo:alpha", False), (f"{JM}/a/Same.java", 5, "demo:alpha", False)}
    assert [h.label for h in res.helpers] == ["Util.run"]
    missing = datapack.lookup(root)["problems"]["missing_functions"]
    assert not [r for r in missing if r["name"] == "demo:hello_world"]


def test_nested_classes_resolve_as_java_does(tmp_path):
    text = """package demo;

class Outer {
    static class A {
        static void run(MinecraftServer s, String name) {
            """ + LOOKUP + """
        }
    }

    static class B {
        static void run(MinecraftServer s, String name) {
            System.out.println(name);
        }

        void m(MinecraftServer s) {
            run(s, "not_a_function");
            A.run(s, "alpha");
            Outer.A.run(s, "beta");
        }
    }

    void top(MinecraftServer s) {
        A.run(s, "gamma");
    }
}
"""
    near = _user("demo", "Near", "", '        Outer.A.run(s, "delta");\n        A.run(s, "not_visible");\n')
    res = datapack_java.scan(tmp_path, {"demo/Outer.java": text, "demo/Near.java": near})
    assert _calls(res) == {("demo/Outer.java", 17, "demo:alpha", False), ("demo/Outer.java", 18, "demo:beta", False),
                           ("demo/Outer.java", 23, "demo:gamma", False), ("demo/Near.java", 5, "demo:delta", False)}


def test_a_reference_tree_helper_does_not_bind_product_calls(tmp_path):
    ref = "original/src/main/java/a/Util.java"
    files = {
        ref: _util("a", LOOKUP).replace("    }\n}\n", '    }\n\n    void own(MinecraftServer s) {\n'
                                                     '        Util.run(s, "alpha");\n    }\n}\n'),
        f"{JM}/a/Util.java": _util("a", "System.out.println(name);"),
        f"{JM}/c/User.java": _user("c", "User", "import a.Util;\n\n", '        Util.run(s, "hello_world");\n'),
    }
    root = tmp_path / "mod"
    _write(root, files)
    setup.add_reference(root, "original")
    res = datapack_java.scan(root, files)
    assert [(c.file, c.line, c.target, c.tree) for c in res.calls] == [(ref, 9, "demo:alpha", "original")]


def test_an_overload_that_delegates_to_the_helper_is_a_helper(tmp_path):
    text = """class ExampleMod {
    static void runFunction(ServerPlayer p, String name) {
        if (p == null) {
            runFunction(p, name);
        }
        p.getServer().getFunctions().get(Identifier.fromNamespaceAndPath("demo", name));
    }

    static void runFunction(ServerPlayer p, String name, int delay) {
        Scheduler.later(delay, () -> runFunction(p, name));
    }

    static void use(ServerPlayer p) {
        runFunction(p, "beta", 20);
        runFunction(p, "alpha");
        Stream.of(1).forEach(x -> ExampleMod
                .runFunction(p, "gamma"));
    }
}
"""
    res = datapack_java.scan(tmp_path, {"ExampleMod.java": text})
    assert _calls(res) == {("ExampleMod.java", 14, "demo:beta", False), ("ExampleMod.java", 15, "demo:alpha", False),
                           ("ExampleMod.java", 17, "demo:gamma", False)}   # a chained call: the line of its name
    assert {(h.label, h.arity, h.source) for h in res.helpers} == {
        ("ExampleMod.runFunction", 2, "body"), ("ExampleMod.runFunction", 3, "forwards to ExampleMod.runFunction")}


def test_only_files_that_can_call_a_helper_are_read(tmp_path):
    texts = {
        "Runner.java": "class Runner {\n    static void run(MinecraftServer s, String name) {\n        " + LOOKUP
                       + "\n    }\n}\n",
        "User.java": 'class User {\n    void m(MinecraftServer s) {\n        Runner.run(s, "alpha");\n    }\n}\n',
        "Busy.java": 'class Busy {\n    void m(Runnable task, Map<String, String> map) {\n        task.run();\n'
                     '        map.get("k");\n        running(1);\n    }\n}\n',
        "Word.java": "class Word {\n    // a file that merely says run(\n    void m() { rerun(); }\n}\n",
    }
    res = datapack_java.scan(tmp_path, texts)
    assert _calls(res) == {("User.java", 3, "demo:alpha", False)}
    assert res.files_read == 2


TABLES = """package com.example.demo;

final class Verbs {
    private static final List<String> KITS = List.of("give_sword", "give_shield");
    private static final List<String> names = List.of("delta");   // hidden by the parameter of `anything`

    static void register(Dispatcher d) {
        String[][] pairs = {
                {"summon", "alpha"}, {"call", "beta"},
                {"again", "alpha"},
        };
        for (String[] e : pairs) {
            final String fn = e[1];
            d.register(literal(e[0]).executes(ctx -> ExampleMod.runFunction(ctx.getPlayer(), fn)));      // MARK table
        }
        for (String kit : KITS) {
            ExampleMod.runFunction(null, kit);      // MARK kits
        }
        for (String[] e : new String[][]{{"x", "gamma"}}) {
            ExampleMod.runFunction(null, e[1]);      // MARK inline
        }
        for (String[] e : pairs) {
            ExampleMod.runFunction(null, e[2]);      // MARK short-row
        }
    }

    static void anything(ServerPlayer p, String[] names) {
        for (String n : names) {
            ExampleMod.runFunction(p, n);      // MARK any-name
        }
    }
}
"""


def test_a_constant_table_binds_each_row_and_the_function_view_names_run_time_sites(tmp_path):
    root = tmp_path / "mod"
    rel = f"{J}/Verbs.java"
    _write(root, {**FILES, rel: TABLES})

    def line(mark: str) -> int:
        return next(i for i, ln in enumerate(TABLES.splitlines(), 1) if f"MARK {mark}" in ln)

    java = datapack.index(root)["java"]
    got = {(c.line, c.target, c.dynamic) for c in java.calls if c.file == rel}
    assert got == {(line("table"), "demo:alpha", False), (line("table"), "demo:beta", False),
                   (line("kits"), "demo:give_sword", False), (line("kits"), "demo:give_shield", False),
                   (line("inline"), "demo:gamma", False),
                   (line("short-row"), "demo:", True), (line("any-name"), "demo:", True)}
    assert not [r for r in datapack.lookup(root)["problems"]["missing_functions"] if r["called_at"].startswith(rel)]
    one = datapack.lookup(root, "function", "demo:beta")
    assert (f"{rel}:{line('table')}", "Verbs.register") in {(c["at"], c["caller"]) for c in one["called_by"]}
    # a site that builds the whole name past the namespace may run any demo: function: the view says so
    assert {c["at"] for c in one["dynamic_any"]} == {f"{rel}:{line('short-row')}", f"{rel}:{line('any-name')}"}
    text = datapack.render(one)
    assert "2 Java site(s) build the name at run time and may run this one too" in text
    assert f"{rel}:{line('any-name')}" in text
    assert "no call in or out found" not in datapack.render(datapack.lookup(root, "function", "demo:delta"))


@pytest.mark.parametrize("code, target", [
    ('server.runCommand(String.format("function demo:%s", kind));', "demo:"),
    ('server.runCommand("function demo:give_%s".formatted(kind));', "demo:give_"),
    ('server.runCommand(String.format(Locale.ROOT, "execute as @a run function %s", id));', ""),
])
def test_format_strings_are_dynamic(tmp_path, code, target):
    text = ("class T {\n    void m(MinecraftServer server) {\n        String kind = pick(), id = pick();\n        "
            + code + "\n    }\n}\n")
    res = datapack_java.scan(tmp_path, {"T.java": text})
    assert [(c.target, c.via, c.dynamic, c.line) for c in res.calls] == [(target, "command-string", True, 4)]


def test_format_strings_fill_constants_and_make_helpers(tmp_path):
    text = """class T {
    static final String NS = "demo";

    static void run(MinecraftServer server, String name) {
        server.runCommand(String.format("function %s:%s", NS, name));
    }

    void m(MinecraftServer server) {
        server.runCommand("function %s:alpha".formatted(NS));
        run(server, "beta");
        LOGGER.info(String.format("function demo:%s failed", "x"));
    }
}
"""
    res = datapack_java.scan(tmp_path, {"T.java": text})
    assert [(c.target, c.via, c.line, c.dynamic) for c in res.calls] == [
        ("demo:alpha", "command-string", 9, False), ("demo:beta", "helper", 10, False)]


def test_execute_if_function_runs_the_function(tmp_path):
    text = """class T {
    void m(MinecraftServer server) {
        server.runCommand("execute if function demo:check run say ok");
        server.runCommand("execute as @a unless function demo:gate run function demo:other");
        server.runCommand("say if function demo:nope run");
    }
}
"""
    res = datapack_java.scan(tmp_path, {"T.java": text})
    assert [(c.target, c.line, c.how) for c in res.calls] == [
        ("demo:check", 3, "execute run"), ("demo:gate", 4, "execute run"), ("demo:other", 4, "execute run")]


def test_each_command_of_a_text_block_has_its_own_line(tmp_path):
    text = ('class T {\n    void m(MinecraftServer server) {\n        run("""\n            say hi\n'
            '            function demo:a\n            function demo:b\n            """);\n    }\n}\n')
    res = datapack_java.scan(tmp_path, {"T.java": text})
    assert [(c.target, c.line) for c in res.calls] == [("demo:a", 5), ("demo:b", 6)]


def test_a_final_identifier_field_is_bound(tmp_path):
    text = """class T {
    private static final ResourceLocation TICK = new ResourceLocation("demo", "tick");

    void m(MinecraftServer server) {
        server.getFunctions().get(TICK);
    }
}
"""
    res = datapack_java.scan(tmp_path, {"T.java": text})
    assert [(c.target, c.via, c.line) for c in res.calls] == [("demo:tick", "identifier", 5)]
    assert res.unresolved == []


def test_a_missing_function_lists_the_run_time_names_that_may_be_it(repo):
    res = datapack.lookup(repo, "function", "demo:give_axe")
    assert res["status"] == "not_found" and len(res["dynamic"]) == 2
    assert "may be this one" in datapack.render(res)


# D71: the Java calls are graph edges, so `when`, `trace` and impact follow them from Java into the datapack.
GRAPH_FILES = {
    f"{DP}/alpha.mcfunction": "say alpha\n",
    f"{DP}/later.mcfunction": "say later\n",
    f"{J}/ExampleMod.java": FILES[f"{J}/ExampleMod.java"],
    f"{J}/Weapon.java": """package com.example.demo;

final class Weapon {
    static void fire(ServerPlayer p) {
        ExampleMod.runFunction(p, "alpha");
    }

    static void arm(MinecraftServer server, CommandSourceStack src) {
        server.getCommands().performPrefixedCommand(src, "schedule function demo:later 2s");
    }

    static void onHit(ServerPlayer p) {
        fire(p);
    }

    static boolean reset(MinecraftServer server, CommandSourceStack src) {
        Identifier id = Identifier.parse("demo:tools/reset");
        if (server.getFunctions().get(id).isEmpty()) {
            return false;
        }
        server.getCommands().performPrefixedCommand(src, "function demo:tools/reset");
        return true;
    }
}
""",
    f"{DP}/tools/reset.mcfunction": "say reset\n",
}


@pytest.fixture(scope="module")
def graph_repo(tmp_path_factory) -> Path:
    import os

    os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")
    from verinoda import search_index, workflow
    from verinoda.store import open_store

    root = tmp_path_factory.mktemp("dpgraph") / "mod"
    for rel, text in GRAPH_FILES.items():
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


def test_java_calls_into_functions_are_graph_edges(graph_repo):
    from verinoda import index

    g = index.load(graph_repo)
    fn = {g.label(n): n for n in g.G.nodes if (g.file(n) or "").endswith(".mcfunction")}
    into = {(g.label(u), g.label(fn[f]), d["relation"], d.get("delay"))
            for f in fn for u, _v, d in g.G.in_edges(fn[f], data=True)}
    assert {(lab.strip(".()"), f, rel, delay) for lab, f, rel, delay in into} == {
        ("fire", "demo:alpha", "calls", None), ("arm", "demo:later", "registers", "40"),
        ("reset", "demo:tools/reset", "calls", None)}
    d = next(d for u, _v, d in g.G.in_edges(fn["demo:alpha"], data=True))
    assert d["source_file"].endswith("Weapon.java") and d["source_location"] == "L5"
    assert "helper ExampleMod.runFunction" in d["context"]


def test_when_follows_java_into_a_function(graph_repo):
    from verinoda import index, when

    g = index.load(graph_repo)
    paths = when.run(g, "demo:alpha")["paths"]
    labels = {g.label(h["from_id"]).strip(".()") for p in paths for h in p if h.get("from_id") in g.G}
    assert {"fire", "onHit"} <= labels
    later = when.run(g, "demo:later")["paths"]
    assert any("40 ticks later" in (h.get("event") or "") for p in later for h in p)


def test_a_function_in_a_subfolder_is_named_by_its_id_and_the_run_is_the_edge(graph_repo):
    """`ns:dir/name` is a function id, not a file path; a method that checks the function exists and then runs it
    is linked at the run (the check's condition is not the run's)."""
    from verinoda import index, when

    g = index.load(graph_repo)
    res = when.run(g, "demo:tools/reset")
    assert res["status"] == "found" and res["symbol"] == "demo:tools/reset"
    hop = next(h for p in res["paths"] for h in p if h.get("to") == "demo:tools/reset" and h.get("from_id"))
    assert hop["at"].endswith("Weapon.java:21") and "isEmpty" not in str(hop.get("condition") or "")
