"""Resource ids between code and data (verinoda.resources): keys, references in context, resolution."""

from __future__ import annotations

from verinoda import resources as rs
from verinoda.resources import KeyMap, Ref, refs_in_line, resource_keys

R = "src/main/resources/"
MOD = [
    "src/main/resources/fabric.mod.json",
    R + "data/glow/function/wisp_death.mcfunction",
    R + "data/glow/function/ritual/start.mcfunction",
    R + "data/glow/advancement/wisp_death.json",
    R + "data/glow/recipe/lantern_staff.json",
    R + "data/glow/tags/block/glow_ore.json",
    R + "data/glow/tags/function/load.json",
    R + "data/glow/worldgen/placed_feature/ember_vein.json",
    R + "assets/glow/models/item/lantern_staff.json",
    R + "assets/glow/models/block/glow_ore.json",
    R + "assets/glow/textures/entity/wisp/wisp_1.png",
    R + "assets/glow/lang/en_us.json",
    "src/main/java/com/glow/Wisp.java",
]


def _km(files=MOD) -> KeyMap:
    return KeyMap.build(files)


def _resolve(text: str, *, data_file: bool, files=MOD) -> list[tuple[str, str, bool]]:
    km = _km(files)
    out = []
    for r in refs_in_line(text, 1, data_file=data_file):
        t = km.resolve(r)
        out += [(r.rid, f.rsplit("/", 2)[-2] + "/" + f.rsplit("/", 1)[-1], km.sure(r, t)) for f, _k in t]
    return out


def test_resource_keys_follow_registries_tags_and_worldgen():
    assert resource_keys(R + "data/glow/function/ritual/start.mcfunction") == [("glow", "ritual/start", "function")]
    assert resource_keys(R + "data/glow/tags/block/glow_ore.json") == [("#glow", "glow_ore", "tags/block")]
    assert resource_keys("data/glow/tags/worldgen/biome/sicak.json") == [("#glow", "sicak", "tags/worldgen/biome")]
    assert resource_keys(R + "data/glow/worldgen/placed_feature/ember_vein.json") == \
        [("glow", "ember_vein", "worldgen/placed_feature")]
    assert resource_keys(R + "assets/glow/models/item/lantern_staff.json") == [
        ("glow", "item/lantern_staff", "models"), ("glow", "models/item/lantern_staff.json", "models"),
        ("glow", "lantern_staff", "models")]
    # not a registry or asset kind: an ordinary data folder
    assert resource_keys("data/processed/train/labels.csv") == []
    assert resource_keys("app/assets/images/icons/search.svg") == []
    assert resource_keys("pack.mcmeta") == []


def test_repositories_without_a_pack_or_mod_manifest_get_no_keys():
    ml = ["data/processed/tags/labels.csv", "src/mlapp/train.py", "assets/site/models/x.json"]
    assert _km(ml).by_key == {}
    assert _resolve('y = df["labels"].values', data_file=False, files=ml) == []
    assert _km(ml + ["datapack/pack.mcmeta"]).by_key  # a data pack's pack.mcmeta is enough


def test_the_context_names_the_registry():
    # a command keyword or a JSON key says which kind of file an id is
    assert _resolve("advancement revoke @s only glow:wisp_death", data_file=True) == \
        [("glow:wisp_death", "advancement/wisp_death.json", True)]
    assert _resolve("function glow:wisp_death", data_file=True) == \
        [("glow:wisp_death", "function/wisp_death.mcfunction", True)]
    assert _resolve('  "parent": "glow:item/lantern_staff",', data_file=True) == \
        [("glow:item/lantern_staff", "item/lantern_staff.json", True)]
    assert _resolve('"feature": "glow:ember_vein"', data_file=True) == \
        [("glow:ember_vein", "placed_feature/ember_vein.json", True)]
    # a tag reference resolves to the tag, never to a same-named model
    assert _resolve("execute if block ~ ~ ~ #glow:glow_ore run say hi", data_file=True) == \
        [("#glow:glow_ore", "block/glow_ore.json", True)]
    assert _resolve("function #glow:load", data_file=True) == [("#glow:load", "function/load.json", True)]
    # an item id (give) names no function or advancement of the same name
    assert _resolve("give @s glow:wisp_death 1", data_file=True) == []


def test_an_id_whose_kind_the_line_does_not_state_is_not_sure():
    got = _resolve('String id = "glow:wisp_death";', data_file=False)
    assert {x[1] for x in got} == {"advancement/wisp_death.json", "function/wisp_death.mcfunction"}
    assert not any(sure for _r, _f, sure in got)


def test_bare_names_count_only_in_id_constructors_and_resource_helpers():
    helper = _resolve('Datapack.run(server, pos, "wisp_death");', data_file=False)
    assert helper == [("wisp_death", "function/wisp_death.mcfunction", False)]  # namespace assumed: never sure
    assert _resolve('fonksiyon(p, le.position(), "wisp_death");', data_file=False) == helper
    assert _resolve('Identifier.of(MOD_ID, "lantern_staff")', data_file=False) != []
    for noise in ('e.entityTags().contains("wisp_death")', 'Set.of("wisp_death", "boss")',
                  'tag.putBoolean("wisp_death", true)', '@Inject(method = "wisp_death", at = @At("HEAD"))',
                  'LOGGER.info("wisp_death")', 'x = "wisp_death"'):
        assert _resolve(noise, data_file=False) == [], noise


def test_explicit_namespace_calls_and_full_asset_paths_resolve():
    assert _resolve('Identifier.of("glow", "ember_vein")', data_file=False) == \
        [("glow:ember_vein", "placed_feature/ember_vein.json", True)]  # one file of one kind: sure
    both = _resolve('Identifier.of("glow", "lantern_staff")', data_file=False)  # a recipe and a model
    assert {f for _r, f, _s in both} >= {"recipe/lantern_staff.json", "item/lantern_staff.json"}
    assert not any(s for _r, _f, s in both)
    assert _resolve('Identifier.fromNamespaceAndPath("glow", "textures/entity/wisp/wisp_1.png")',
                    data_file=False) == [("glow:textures/entity/wisp/wisp_1.png", "wisp/wisp_1.png", True)]


def test_lang_keys_and_unrelated_colons():
    assert _resolve('"item.glow.lantern_staff": "Lantern Staff",', data_file=True) == \
        [("glow:lantern_staff", "item/lantern_staff.json", True)]
    for text in ("at 12:30 the server", "see https://example.com/x", 'path = "C:/games/mc"', "minecraft:stone"):
        assert _resolve(text, data_file=True) == [], text


def test_ref_rid():
    assert Ref(1, "#glow", "glow_ore", "id").rid == "#glow:glow_ore"
    assert Ref(1, "", "wisp_death", "bare").rid == "wisp_death"
    assert rs.MANIFESTS >= {"fabric.mod.json", "pack.mcmeta", "mods.toml"}
