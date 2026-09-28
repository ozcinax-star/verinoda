"""Minecraft datapacks: function calls in the graph, tags and objectives across mcfunction and Java (D52)."""

from __future__ import annotations

import json
import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, datapack, index, search_index, when, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

DP = "src/main/resources/data/wings/function"

FILES = {
    "src/main/resources/data/minecraft/tags/function/tick.json": '{"values": ["wings:tick"]}',
    f"{DP}/tick.mcfunction": """# every tick
execute if score #angel w_state matches 1 run function wings:reveal
schedule function wings:fold 40t
scoreboard players add #angel w_timer 1
""",
    f"{DP}/reveal.mcfunction": """tag @e[tag=angel,limit=1] add wings_broken
scoreboard players set #angel w_state 2
scoreboard players set #angel w_orphan 5
summon marker ~ ~ ~ {Tags:["wing_marker"]}
""",
    f"{DP}/fold.mcfunction": """kill @e[tag=wing_marker]
execute if score #angel w_timer matches 100.. run function wings:missing
""",
    "src/main/java/com/example/wings/Wings.java": """package com.example.wings;

public final class Wings {
    public static final String BROKEN = "wings_broken";
    public static final String HUNTER = "hunter_mark";

    static boolean broken(Entity e) {
        return e.entityTags().contains(BROKEN);
    }

    static boolean hunted(Entity e) {
        return e.entityTags().contains(HUNTER);
    }

    static int score(Server server, String objective) {
        Objective o = server.getScoreboard().getObjective(objective);
        return server.getScoreboard().getPlayerScoreInfo(null, o).value();
    }

    static boolean revealing(Server server) {
        return score(server, "w_state") == 1;
    }
}
""",
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("datapack") / "mod"
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


def test_parse_function():
    calls, tags, objs = datapack.parse_function(FILES[f"{DP}/tick.mcfunction"])
    assert calls == [(2, "wings:reveal", "execute run", None), (3, "wings:fold", "schedule", "40t")]
    assert (2, "w_state", "read") in objs and (4, "w_timer", "write") in objs
    _c, tags, _o = datapack.parse_function(FILES[f"{DP}/reveal.mcfunction"])
    assert (1, "angel", "check") in tags and (1, "wings_broken", "add") in tags and (4, "wing_marker", "add") in tags


def test_index_and_problems(repo):
    ix = datapack.index(repo)
    assert set(ix["functions"]) == {"wings:tick", "wings:reveal", "wings:fold"}
    assert ix["functions"]["wings:tick"].events == ["#minecraft:tick"]
    broken = {(s.lang, s.kind) for s in ix["tags"]["wings_broken"]}
    assert broken == {("mcfunction", "add"), ("java", "check")}           # the constant is resolved
    assert {(s.lang, s.kind) for s in ix["objectives"]["w_state"]} >= {("java", "read"), ("mcfunction", "write")}
    pr = datapack.problems(ix)
    assert [r["name"] for r in pr["tags_checked_never_added"]] == ["angel", "hunter_mark"]
    assert [r["name"] for r in pr["scores_written_never_read"]] == ["w_orphan"]
    assert [r["name"] for r in pr["missing_functions"]] == ["wings:missing"]


def test_graph_edges_and_when(repo):
    g = index.load(repo)
    lab = {g.label(n): n for n in g.G.nodes if g.file(n) and g.file(n).endswith(".mcfunction")}
    assert set(lab) == {"wings:tick", "wings:reveal", "wings:fold"}
    rels = {(g.label(u), g.label(v), d["relation"]) for u, v, d in g.edges({"calls", "registers"})
            if u in lab.values()}
    assert rels == {("wings:tick", "wings:reveal", "calls"), ("wings:tick", "wings:fold", "registers")}
    d = next(d for u, v, d in g.edges({"registers"}) if g.label(v) == "wings:fold")
    assert d["delay"] == "40"
    res = when.run(g, "wings:fold")
    assert any(h.get("event") == "40 ticks later" for p in res["paths"] for h in p)
    reveal = when.run(g, "wings:reveal")["paths"]
    assert any("every server tick" in (h.get("event") or "") for p in reveal for h in p)
    assert not any("nothing in the project calls" in (h.get("why") or "") for p in reveal for h in p)


def test_cli(repo, capsys):
    assert cli.main(["datapack", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "tags checked but never added (2):" in out and "hunter_mark" in out
    assert cli.main(["datapack", "tag", "wings_broken", "--repo", str(repo), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["sites"]) == 2
    assert cli.main(["datapack", "function", "wings:fold", "--repo", str(repo)]) == 0
    assert "called by wings:tick" in capsys.readouterr().out


def _dangling_link(link, target) -> bool:
    """A junction (Windows) or symlink (elsewhere) to ``target``, which is then removed; False when not possible."""
    import os
    import shutil

    target.mkdir(parents=True)
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, AttributeError, ImportError):
        return False
    shutil.rmtree(target)
    return True


def test_links_whose_target_is_gone_are_skipped_not_fatal(tmp_path):
    """A node_modules package linked to a folder that has since moved (npm workspaces, a copied project): the
    datapack reader skips the link instead of stopping."""
    root = tmp_path / "mod"
    fn = root / "data" / "ns" / "function"
    fn.mkdir(parents=True)
    (fn / "a.mcfunction").write_text("tag @s add seen\nexecute if entity @s[tag=seen] run say hi\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "A.java").write_text("class A {}\n", encoding="utf-8")
    nm = root / "tools" / "x" / "node_modules" / "@scope"
    nm.mkdir(parents=True)
    made = [_dangling_link(nm / name, tmp_path / "moved" / name) for name in ("plugin", "shared")]
    other = root / "tools" / "stray"
    made.append(_dangling_link(other, tmp_path / "moved" / "stray"))
    if not any(made):
        pytest.skip("links cannot be made here")
    res = datapack.lookup(root, None, None)
    assert res["functions"] == 1 and res["status"] == "found"


# D70 (GitHub issue #2): tags added through a class's own constant, a conditional, the live tag set, or a name built
# at run time. Two classes give ETIKET different values; each call site reads its own class's (or the one it names).
TAGS = {
    "src/main/java/mod/Croatoan.java": """package mod;

public class Croatoan {
    public static final String ETIKET = "croat";

    void mark(Villager sakin) {
        sakin.addTag(ETIKET);
    }
}
""",
    "src/main/java/mod/Musallat.java": """package mod;

public class Musallat {
    private static final String ETIKET = "musallat";
    static final String ESKI_KOYLU = "musallat_koylu";
    static final String ESKI_SAKIN = "musallat_sakin";
    static final String KORUMALI = "musallat_korumali";

    void haunt(Entity vucut, boolean koyluydu) {
        vucut.addTag(koyluydu ? ESKI_KOYLU : ESKI_SAKIN);
        vucut.addTag(ETIKET);
    }

    boolean was(Entity e) {
        return e.entityTags().contains(ESKI_KOYLU) || e.entityTags().contains(ESKI_SAKIN)
            || e.entityTags().contains(ETIKET);
    }

    boolean guarded(Entity e) {
        return e.entityTags().contains(KORUMALI);
    }
}
""",
    "src/main/java/mod/LuciferTeklifi.java": """package mod;

public class LuciferTeklifi {
    boolean taken(Entity e) {
        return e.entityTags().contains(Croatoan.ETIKET);
    }
}
""",
    "src/main/java/mod/Atlilar.java": """package mod;

public class Atlilar {
    void mount(Entity at, Rider a) {
        at.addTag(a.tag + "_at");
    }

    boolean deathHorse(Entity e) {
        return e.entityTags().contains("olum_at");
    }

    static void tagAll(Entity e, String tag) {
        e.addTag(tag);
    }
}
""",
    "src/main/java/mod/Inis.java": """package mod;

public class Inis {
    static final String HURDA_ETIKET = "yikim_hurda";

    void wreck(Entity araba) {
        araba.entityTags().add(HURDA_ETIKET);
    }

    boolean wrecked(Entity e) {
        return e.entityTags().contains(HURDA_ETIKET);
    }

    void repair(Entity araba) {
        araba.getTags().remove(HURDA_ETIKET);
    }
}
""",
}


@pytest.fixture()
def tag_repo(tmp_path) -> Path:
    root = tmp_path / "mod"
    for rel, text in TAGS.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    return root


def _line(rel: str, needle: str) -> str:
    return f"{rel}:{next(i for i, t in enumerate(TAGS[rel].splitlines(), 1) if needle in t)}"


def test_tags_added_through_constants_conditionals_and_the_live_set(tag_repo):
    ix = datapack.index(tag_repo, java_calls=False)
    sites = lambda name: {(s.at, s.kind) for s in ix["tags"].get(name, [])}  # noqa: E731
    croat = sites("croat")
    assert (_line("src/main/java/mod/Croatoan.java", "sakin.addTag"), "add") in croat          # own class's ETIKET
    assert (_line("src/main/java/mod/LuciferTeklifi.java", "Croatoan.ETIKET"), "check") in croat  # Owner.NAME
    assert (_line("src/main/java/mod/Musallat.java", "addTag(ETIKET)"), "add") in sites("musallat")
    koylu = _line("src/main/java/mod/Musallat.java", "koyluydu ?")
    assert (koylu, "add") in sites("musallat_koylu") and (koylu, "add") in sites("musallat_sakin")
    inis = "src/main/java/mod/Inis.java"
    assert sites("yikim_hurda") >= {(_line(inis, "entityTags().add"), "add"), (_line(inis, "getTags().remove"), "remove"),
                                    (_line(inis, "contains(HURDA"), "check")}


def test_a_checked_tag_a_built_name_may_add_is_marked_not_dropped(tag_repo):
    pr = datapack.problems(datapack.index(tag_repo, java_calls=False))
    rows = {r["name"]: r for r in pr["tags_checked_never_added"]}
    assert set(rows) == {"musallat_korumali", "olum_at"}         # croat, musallat_koylu, yikim_hurda are added
    built = _line("src/main/java/mod/Atlilar.java", "_at\")")
    assert rows["olum_at"]["maybe_added_by"] == [{"at": built, "pattern": "*_at"}]
    assert "maybe_added_by" not in rows["musallat_korumali"]      # nothing could add it: never added
    assert pr["tags_added_dynamically"] == [{"at": built, "pattern": "*_at"}]   # a helper's own parameter is not one


def test_datapack_summary_and_tag_lookup_render_the_dynamic_add(tag_repo, capsys):
    assert cli.main(["datapack", "--repo", str(tag_repo)]) == 0
    out = capsys.readouterr().out
    built = _line("src/main/java/mod/Atlilar.java", "_at\")")
    assert "tags checked but never added (2):" in out
    assert f"olum_at  {_line('src/main/java/mod/Atlilar.java', 'contains(')}  (maybe added by {built}: *_at)" in out
    assert f"tag names Java builds at run time (1): {built} adds *_at" in out
    assert cli.main(["datapack", "tag", "croat", "--repo", str(tag_repo)]) == 0
    out = capsys.readouterr().out
    assert f"add    java       {_line('src/main/java/mod/Croatoan.java', 'sakin.addTag')}" in out


def test_values_of_a_string_expression():
    known = {"A": "a", "B": "b", "PRE": "pre_"}
    const = lambda e, d: known.get(e.rsplit(".", 1)[-1])  # noqa: E731
    v = lambda e: datapack._values(e, const)  # noqa: E731
    assert v('"x"') == ({"x"}, []) and v("(A)") == ({"a"}, [])
    assert v("c ? A : B") == ({"a", "b"}, [])
    assert v("c ? A : d ? B : \"z\"") == ({"a", "b", "z"}, [])
    assert v("c ? d ? A : B : \"z\"") == ({"a", "b", "z"}, [])
    assert v('c ? "q?" : "r:"') == ({"q?", "r:"}, [])            # a ? or : in a literal is not the operator
    assert v('PRE + "x"') == ({"pre_x"}, []) and v('PRE + (c ? A : B)') == ({"pre_a", "pre_b"}, [])
    assert v('a.tag + "_at"') == (set(), ["*_at"]) and v('PRE + i + "_" + j') == (set(), ["pre_*_*"])
    assert v("name") == (set(), ["*"]) and v('String.format("%s_x", n)') == (set(), ["*"])


def test_constants_bind_where_java_binds_them():
    texts = {
        "a/Tags.java": 'package a;\npublic final class Tags {\n    public static final String BASE = "mod_";\n'
                       '    public static final String HURDA = BASE + "hurda";\n'
                       '    public static final String ETIKET = "tags";\n}\n',
        "b/Uses.java": 'package b;\nimport static a.Tags.HURDA;\nclass Uses {\n    static final String ETIKET = "uses";\n'
                       '    static class Inner {\n        static final String ETIKET = "inner";\n'
                       '        void f() { x(ETIKET); }\n    }\n    void g() { x(ETIKET); x(HURDA); }\n}\n',
        "c/Loop.java": 'class Loop {\n    static final String SELF = SELF + "x";\n    // static final String GHOST = "g";\n}\n',
    }
    cx = datapack._Consts(texts)
    at = lambda f, needle: texts[f].index(needle)  # noqa: E731
    assert cx.value("HURDA", "a/Tags.java", at("a/Tags.java", "HURDA")) == "mod_hurda"
    assert cx.value("ETIKET", "b/Uses.java", at("b/Uses.java", "void f")) == "inner"
    assert cx.value("ETIKET", "b/Uses.java", at("b/Uses.java", "void g")) == "uses"
    assert cx.value("HURDA", "b/Uses.java", at("b/Uses.java", "void g")) == "mod_hurda"   # the static import
    assert cx.value("Tags.ETIKET", "b/Uses.java", 0) == "tags"
    assert cx.value("ETIKET", "c/Loop.java", 0) is None                  # three classes disagree: not guessed
    assert cx.value("SELF", "c/Loop.java", at("c/Loop.java", "SELF")) is None and cx.value("GHOST", "c/Loop.java", 0) is None
    assert cx.unique() == {"BASE": "mod_", "HURDA": "mod_hurda"}


def test_a_tag_call_in_a_comment_or_string_is_not_one(tmp_path):
    src = tmp_path / "m" / "src" / "A.java"
    src.parent.mkdir(parents=True)
    src.write_text('class A {\n    // e.addTag("ghost");\n    String s = "e.addTag(\\"ghost2\\")";\n'
                   '    /* e.entityTags().add("ghost3"); */ void f(Entity e) { e.entityTags().add("real"); }\n}\n',
                   encoding="utf-8")
    ix = datapack.index(tmp_path / "m", java_calls=False)
    assert set(ix["tags"]) == {"real"} and ix["tag_patterns"] == []
