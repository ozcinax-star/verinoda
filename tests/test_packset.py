"""Packs and mods loaded together: resource collisions with both sources, unmet mod dependencies."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from verinoda import cli, datapack, packset

RES = "src/main/resources"
PACK = "packs/wings_extra"

FABRIC = """{
  "schemaVersion": 1,
  "id": "wings",
  "version": "${version}",
  "depends": {
    "fabricloader": ">=0.15",
    "minecraft": "~1.21",
    "fabric-api": ">=0.100",
    "fabric-api-base": "*",
    "trinkets": "*"
  },
  "breaks": {
    "optifabric": "*"
  }
}
"""

FORGE = """modLoader="javafml"
loaderVersion="[47,)"

[[mods]]
modId="wings"
version="1.0.0"

[[dependencies.wings]]
    modId="forge"
    mandatory=true
    versionRange="[47,)"
[[dependencies.wings]]
    modId="curios"
    mandatory=true
    versionRange="[5.0,6.0)"
"""

FILES = {
    f"{RES}/fabric.mod.json": FABRIC,
    f"{RES}/data/wings/recipe/feather.json": '{"type": "minecraft:crafting_shaped", "result": {"id": "wings:feather"}}',
    f"{RES}/data/wings/loot_table/nest.json": '{"pools": []}',
    f"{RES}/data/wings/tags/item/light.json": '{"values": ["wings:feather"]}',
    f"{RES}/data/wings/tags/item/soft.json": '{"values": ["wings:feather"]}',
    f"{RES}/assets/wings/lang/en_us.json": '{"item.wings.feather": "Feather"}',
    f"{RES}/assets/wings/textures/item/feather.png": "PNG-ours",
    f"{RES}/data/wings/recipe/wing.json": '{"fabric": true}',
    # a datapack beside the mod: a different recipe at the same path, the same loot table, a replacing tag
    f"{PACK}/pack.mcmeta": '{"pack": {"pack_format": 48, "description": "extra"}}',
    f"{PACK}/data/wings/recipe/feather.json": '{"type": "minecraft:crafting_shapeless", "result": {"id": "x:y"}}',
    f"{PACK}/data/wings/loot_table/nest.json": '{"pools": []}',
    f"{PACK}/data/wings/tags/item/light.json": '{"replace": true, "values": []}',
    f"{PACK}/data/wings/tags/item/soft.json": '{"values": ["x:y"]}',
    f"{PACK}/assets/wings/lang/en_us.json": '{"item.wings.feather": "Plume"}',
    # the Forge build of the same mod: never loaded with the Fabric one
    f"forge/{RES}/META-INF/mods.toml": FORGE,
    f"forge/{RES}/data/wings/recipe/wing.json": '{"forge": true}',
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


def _jar(path: Path, files: dict[str, bytes | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)


def _nested(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("packset") / "mod"
    _write(root, FILES)
    return root


@pytest.fixture(scope="module")
def mods(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("packset_mods") / "mods"
    _jar(folder / "fabric-api-0.90.0.jar", {
        "fabric.mod.json": json.dumps({"id": "fabric-api", "version": "0.90.0+1.21",
                                       "jars": [{"file": "META-INF/jars/base.jar"}]}),
        "META-INF/jars/base.jar": _nested({"fabric.mod.json": json.dumps({"id": "fabric-api-base",
                                                                          "version": "0.4.0"})}),
    })
    _jar(folder / "optifabric-1.0.jar", {
        "fabric.mod.json": json.dumps({"id": "optifabric", "version": "1.0.0"}),
        "assets/wings/textures/item/feather.png": b"PNG-theirs",
        "assets/wings/lang/en_us.json": '{"item.wings.feather": "Pen"}',
    })
    return folder


def test_sources(repo):
    srcs = {s.label: s for s in packset.sources(repo)}
    assert set(srcs) == {RES, PACK, f"forge/{RES}"}
    assert [m.id for m in srcs[RES].mods] == ["wings"] and srcs[RES].mods[0].version is None
    assert srcs[f"forge/{RES}"].mods[0].loader == "forge"
    assert srcs[PACK].mods == [] and f"data/wings/recipe/feather.json" in srcs[PACK].files


def test_collisions_in_the_repository(repo):
    found, copies = packset.collisions(packset.sources(repo))
    paths = [r["path"] for r in found]
    # the recipe differs, the tag replaces; lang merges, a tag without replace merges, the Forge build is apart
    assert paths == ["data/wings/recipe/feather.json", "data/wings/tags/item/light.json"]
    feather = found[0]
    assert feather["status"] == "verified"
    assert [s["at"] for s in feather["sources"]] == [f"{PACK}/data/wings/recipe/feather.json",
                                                     f"{RES}/data/wings/recipe/feather.json"]
    assert [s.get("mod") for s in feather["sources"]] == [None, "wings"]
    assert [r["path"] for r in copies] == ["data/wings/loot_table/nest.json"]


def test_dependencies_without_the_mod_set(repo):
    dep = packset.dependencies(packset.sources(repo), checked=False)
    assert dep["problems"] == []  # the repository alone is not the set the game loads
    assert sorted(r["dependency"] for r in dep["external"]) == ["curios", "fabric-api", "fabric-api-base",
                                                                "trinkets"]
    row = next(r for r in dep["external"] if r["dependency"] == "fabric-api")
    lines = FABRIC.splitlines()
    assert row["at"] == f"{RES}/fabric.mod.json:{lines.index(next(s for s in lines if 'fabric-api' in s)) + 1}"
    curios = next(r for r in dep["external"] if r["dependency"] == "curios")
    assert curios["at"] == f"forge/{RES}/META-INF/mods.toml:13" and curios["wants"] == "[5.0,6.0)"


def test_with_a_mods_folder(repo, mods):
    res = packset.check(repo, [str(mods)])
    labels = [s["source"] for s in res["sources"]]
    assert f"{mods.as_posix()}/optifabric-1.0.jar" in labels
    tex = next(r for r in res["collisions"] if r["path"] == "assets/wings/textures/item/feather.png")
    assert [s["at"] for s in tex["sources"]] == [
        f"{RES}/assets/wings/textures/item/feather.png",
        f"{mods.as_posix()}/optifabric-1.0.jar!/assets/wings/textures/item/feather.png"]
    assert not any(r["path"].endswith("lang/en_us.json") for r in res["collisions"])
    rows = {(r["kind"], r["mod"], r["dependency"]): r for r in res["dependencies"]["problems"]}
    assert set(rows) == {("version", "wings", "fabric-api"), ("missing", "wings", "trinkets"),
                         ("breaks", "wings", "optifabric")}
    # curios is a dependency of the Forge build, and the folder holds only Fabric mods: not checked, not missing
    ext = res["dependencies"]["external"]
    assert [(r["dependency"], r["loader"]) for r in ext] == [("curios", "forge")]
    assert "0.90.0+1.21" in rows[("version", "wings", "fabric-api")]["found"]
    light = next(r for r in res["collisions"] if r["path"] == "data/wings/tags/item/light.json")
    assert light["replace"] == [f"{PACK}/data/wings/tags/item/light.json"]
    assert "dropped only when they load below it" in packset.collision_line(light)
    assert all(r["status"] == "strong_inference" for r in rows.values())
    # fabric-api-base comes from a jar fabric-api bundles: met, not missing
    assert ("missing", "wings", "fabric-api-base") not in rows


@pytest.mark.parametrize("version,wants,style,ok", [
    ("1.2.3", [">=1.2"], "semver", True),
    ("0.90.0+1.21", [">=0.100"], "semver", False),
    ("1.21.4", ["~1.21"], "semver", True),
    ("1.22.0", ["~1.21"], "semver", False),
    ("1.9.0", ["^1.2"], "semver", True),
    ("2.0.0", ["^1.2"], "semver", False),
    ("1.20.4", ["1.20.x"], "semver", True),
    ("1.5", [">=1.0 <1.4", "1.5"], "semver", True),
    ("1.0.0-beta.1", [">=1.0.0"], "semver", False),
    ("0.92.0+1.20.1", ["^0.90.0"], "semver", True),     # ^ keeps the major only, 0.x included
    ("1.0.0", ["^0.90.0"], "semver", False),
    ("1.0.0-beta.10", [">=1.0.0-beta.9"], "semver", True),  # numeric identifiers compare as numbers
    ("11.0.0-beta.2", [">=11.0.0-beta.10"], "semver", False),
    ("1.20.1-47.2.0", ["[1.20.1,)"], "maven", True),    # a Maven suffix that is not a qualifier sorts above
    ("1.2.3-forge", ["[1.2.3,)"], "maven", True),
    ("1.2.3-beta.1", ["[1.2.3,)"], "maven", False),
    ("1.2.3-SNAPSHOT", ["[1.2.3,)"], "maven", False),
    ("1.5", ["[1.0,2.0)"], "maven", True),
    ("2.0", ["[1.0,2.0)"], "maven", False),
    ("47.1.0", ["[47,)"], "maven", True),
    ("3.0", ["(,1.0],[2.0,)"], "maven", True),
    ("1.0", ["${range}"], "semver", None),
    ("dev", [">=1"], "semver", None),
])
def test_satisfies(version, wants, style, ok):
    assert packset.satisfies(version, wants, style) is ok


def test_datapack_summary_reports_both_sources(repo, mods, capsys):
    res = datapack.lookup(repo)
    assert res["status"] == "found"
    assert [r["path"] for r in res["problems"]["pack_collisions"]] == ["data/wings/recipe/feather.json",
                                                                      "data/wings/tags/item/light.json"]
    assert cli.main(["datapack", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "resource collisions across packs and mods (2):" in out
    assert (f"data/wings/recipe/feather.json: {PACK}/data/wings/recipe/feather.json <> "
            f"{RES}/data/wings/recipe/feather.json (wings)") in out
    assert "dependencies outside the repository, not checked (4" in out
    assert cli.main(["datapack", "packs", "--repo", str(repo), "--with", str(mods), "--json"]) == 0
    js = json.loads(capsys.readouterr().out)
    assert js["kind"] == "packs" and len(js["collisions"]) == 3
    assert cli.main(["datapack", "packs", "--repo", str(repo), "--with", str(mods)]) == 0
    out = capsys.readouterr().out
    assert "wings needs fabric-api >=0.100: found 0.90.0+1.21" in out
    assert "wings breaks with optifabric any" in out


def test_no_pack(tmp_path):
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    assert datapack.lookup(tmp_path)["status"] == "no_datapack"
    assert datapack.lookup(tmp_path, "packs")["status"] == "no_datapack"


@pytest.mark.parametrize("text,loader", [
    ('{"schemaVersion": 1, "id": "x", "depends": ["fabric-api"]}', "fabric"),
    ('{"id": "x", "depends": "a"}', "fabric"),
    ('{"id": "x", "breaks": [1], "provides": "y"}', "fabric"),
    ('{"quilt_loader": {"id": "x", "depends": 5}}', "quilt"),
    ('{"quilt_loader": {"id": "x", "provides": 5, "breaks": {"a": "*"}}}', "quilt"),
    ('mods = 5', "forge"),
    ('[[mods]]\nmodId = "x"\n[dependencies]\nx = 5', "forge"),
])
def test_malformed_manifest_declares_nothing(text, loader):
    mods = packset.parse_manifest(text, loader, "f")
    assert all(m.depends == [] and m.breaks == [] for m in mods)


def test_malformed_manifest_keeps_the_summary(tmp_path):
    _write(tmp_path, {f"{RES}/data/ns/function/a.mcfunction": "say hi\n",
                      f"{RES}/fabric.mod.json": '{"schemaVersion": 1, "id": "x", "depends": ["fabric-api"]}'})
    assert cli.main(["datapack", "--repo", str(tmp_path)]) == 0


def test_font_definitions_merge(tmp_path):
    folder = tmp_path / "mods"
    for n in "ab":
        _jar(folder / f"emoji-{n}.jar", {"fabric.mod.json": json.dumps({"id": f"emoji_{n}", "version": "1.0.0"}),
                                         "assets/minecraft/font/default.json": f'{{"providers": ["{n}"]}}',
                                         "assets/minecraft/font/x.ttf": n})
    (tmp_path / "empty").mkdir()
    found, _ = packset.collisions(packset.sources(tmp_path / "empty", [str(folder)]))
    assert [r["path"] for r in found] == ["assets/minecraft/font/x.ttf"]


def test_other_loader_does_not_meet_a_dependency(tmp_path):
    folder = tmp_path / "mods"
    _jar(folder / "fabric-api.jar", {"fabric.mod.json": json.dumps({"id": "fabric-api", "version": "0.92.0"})})
    _jar(folder / "trinkets-forge.jar", {"META-INF/mods.toml": '[[mods]]\nmodId="trinkets"\nversion="3.0.0"\n'})
    repo = tmp_path / "r"
    _write(repo, {f"{RES}/fabric.mod.json": json.dumps({"id": "m", "depends": {"trinkets": "*",
                                                                               "fabric-api": "^0.90.0"}})})
    dep = packset.check(repo, [str(folder)])["dependencies"]
    assert [(r["kind"], r["dependency"]) for r in dep["problems"]] == [("missing", "trinkets")]


def test_unreadable_with_path_checks_nothing(repo, tmp_path, capsys):
    res = packset.check(repo, [str(tmp_path / "no such mods")])
    assert res["dependencies"]["problems"] == []
    assert {r["dependency"] for r in res["dependencies"]["external"]} >= {"trinkets", "curios"}
    assert res["unreadable"] == [(tmp_path / "no such mods").as_posix()]
    summary = datapack.lookup(repo, with_paths=[str(tmp_path / "no such mods")])
    assert summary["unreadable"] and summary["problems"]["mod_dependencies"] == []
    assert summary["problems"]["pack_copies"][0]["path"] == "data/wings/loot_table/nest.json"
    assert cli.main(["datapack", "--repo", str(repo), "--with", str(tmp_path / "no such mods")]) == 0
    out = capsys.readouterr().out
    assert "--with sources that could not be read (1)" in out
    assert "not checked (4; no fabric, forge mod was read from --with)" in out


def test_with_is_refused_on_a_lookup(repo):
    with pytest.raises(SystemExit):
        cli.main(["datapack", "tag", "foo", "--repo", str(repo), "--with", "mods"])
