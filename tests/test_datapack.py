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
