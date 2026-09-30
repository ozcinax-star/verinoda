"""Minecraft translation keys: locales against the default one, the code against the default file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinoda import cli, langkeys

EN = """{
  "item.gem.ruby": "Ruby",
  "block.gem.ruby_ore": "Ruby Ore",
  "tooltip.gem.charges": "%s of %s charges",
  "gui.gem.title": "Gem Bench",
  "message.gem.found": "Found %1$s at %2$s",
  "message.gem.old": "Nobody says this",
  "gui.gem.title": "Gem Workbench",
  "advancements.gem.root.title": "Gems",
  "tooltip.gem.level.1": "Level one"
}
"""

DE = """{
  "item.gem.ruby": "Rubin",
  "block.gem.ruby_ore": "Rubinerz",
  "tooltip.gem.charges": "%s Ladungen",
  "message.gem.found": "%2$s: %1$s gefunden",
  "message.gem.old": "Sagt niemand",
  "advancements.gem.root.title": "Edelsteine",
  "tooltip.gem.level.1": "Stufe eins",
  "item.gem.sapphire": "Saphir"
}
"""

JAVA = """package com.example.gem;

public final class GemItems {
    public static final Item RUBY = register("ruby", new Item(new Item.Settings()));
    public static final Block RUBY_ORE = Blocks.register(Identifier.of("gem", "ruby_ore"));

    void tooltip(List<Text> lines, int level, int n, int max) {
        lines.add(Text.translatable("tooltip.gem.charges", n, max));
        lines.add(Text.translatable("tooltip.gem.level." + level));
        player.sendMessage(Text.translatable("message.gem.found", what, where));
        screen.setTitle(Text.translatable("gui.gem.title"));
        screen.addButton(Text.translatable("gui.gem.missing"));
        screen.addButton(Text.translatable("gui.done"));
    }
}
"""

ADVANCEMENT = """{"display": {"title": {"translate": "advancements.gem.root.title"}, "icon": {"id": "gem:ruby"}}}"""


@pytest.fixture()
def repo(tmp_path) -> Path:
    root = tmp_path / "mod"
    for rel, text in {"src/main/resources/assets/gem/lang/en_us.json": EN,
                      "src/main/resources/assets/gem/lang/de_de.json": DE,
                      "src/main/java/com/example/gem/GemItems.java": JAVA,
                      "src/main/resources/data/gem/advancement/root.json": ADVANCEMENT}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _by(res: dict) -> dict[tuple[str, str | None], dict]:
    return {(f["kind"], f["key"]): f for f in res["findings"]}


def test_the_lang_file_reader_keeps_every_line_of_a_key_written_twice(repo):
    lf = langkeys.read_lang(repo, "src/main/resources/assets/gem/lang/en_us.json")
    assert lf.error is None
    assert lf.keys["gui.gem.title"] == [(5, "Gem Bench"), (8, "Gem Workbench")]
    assert lf.line("item.gem.ruby") == 2


def test_placeholders_are_argument_indexes_with_their_conversion():
    assert langkeys.placeholders("%s of %s") == {1: "s", 2: "s"}
    assert langkeys.placeholders("%2$s: %1$s") == {1: "s", 2: "s"}
    assert langkeys.placeholders("100%% sure, %d left") == {1: "s"}   # the game loads %d as %s
    assert langkeys.placeholders("none") == {}


def test_each_locale_finding_cites_both_files(repo):
    en = "src/main/resources/assets/gem/lang/en_us.json"
    de = "src/main/resources/assets/gem/lang/de_de.json"
    got = _by(langkeys.check(repo))
    miss = got[("missing_in_locale", "gui.gem.title")]
    assert miss["at"] == f"{en}:5" and miss["other_at"] == de and miss["status"] == "statically_verified"
    extra = got[("extra_in_locale", "item.gem.sapphire")]
    assert extra["at"] == f"{de}:9" and extra["other_at"] == en
    ph = got[("placeholder_mismatch", "tooltip.gem.charges")]
    assert ph["at"] == f"{de}:4" and ph["other_at"] == f"{en}:4"
    assert "%1$s in de_de, %1$s %2$s in en_us" in ph["why"]
    # reordered positional arguments are the same placeholders
    assert ("placeholder_mismatch", "message.gem.found") not in got
    dup = got[("duplicate_key", "gui.gem.title")]
    assert dup["at"] == f"{en}:8" and dup["other_at"] == f"{en}:5"


def test_the_code_against_the_default_file(repo):
    got = _by(langkeys.check(repo))
    missing = got[("missing_key", "gui.gem.missing")]
    assert missing["at"] == "src/main/java/com/example/gem/GemItems.java:12"
    assert missing["other_at"] == "src/main/resources/assets/gem/lang/en_us.json"
    assert ("missing_key", "gui.done") not in got               # a vanilla key: no segment names the mod
    unused = {k for kind, k in got if kind == "unused_key"}
    # named in full (Java, advancement JSON), built from a prefix, from a registered id: all used
    assert unused == {"message.gem.old"}
    assert got[("unused_key", "message.gem.old")]["status"] == "strong_inference"


def test_a_clean_pack_and_a_broken_file(repo):
    de = repo / "src/main/resources/assets/gem/lang/de_de.json"
    de.write_text("{\n  \"item.gem.ruby\": \"Rubin\",\n  \"oops\" \"x\"\n}\n", encoding="utf-8")
    got = _by(langkeys.check(repo, unused=False))
    bad = got[("invalid_file", None)]
    assert bad["at"] == "src/main/resources/assets/gem/lang/de_de.json:3"
    assert not any(kind == "missing_in_locale" for kind, _ in got)   # a file not read is not compared


def test_legacy_lang_files_and_another_default(tmp_path):
    root = tmp_path / "old"
    for rel, text in {"assets/gem/lang/en_US.lang": "# comment\nitem.gem.ruby.name=Ruby\ngui.gem.a=%s\n",
                      "assets/gem/lang/ru_RU.lang": "item.gem.ruby.name=Rubin\ngui.gem.a=a\n"}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    got = _by(langkeys.check(root, unused=False))
    assert got[("placeholder_mismatch", "gui.gem.a")]["other_at"] == "assets/gem/lang/en_US.lang:3"
    swapped = _by(langkeys.check(root, "ru_ru", unused=False))
    assert swapped[("placeholder_mismatch", "gui.gem.a")]["other_at"] == "assets/gem/lang/ru_RU.lang:2"


def test_cli(repo, capsys, tmp_path):
    assert cli.main(["lang", "--repo", str(repo)]) == 3
    out = capsys.readouterr().out
    assert "missing_in_locale [statically_verified]" in out
    assert "<- src/main/resources/assets/gem/lang/de_de.json" in out
    assert cli.main(["lang", "--repo", str(repo), "--json", "--no-unused"]) == 3
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "found" and not any(f["kind"] == "unused_key" for f in res["findings"])
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["lang", "--repo", str(empty)]) == 2


def _tree(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8")
    return root


def test_a_key_built_at_run_time_is_not_a_missing_key(repo, tmp_path):
    # the fixture's `"tooltip.gem.level." + level` is a prefix, not a key
    assert not any(f["kind"] == "missing_key" and f["key"] != "gui.gem.missing"
                   for f in langkeys.check(repo, unused=False)["findings"])
    root = _tree(tmp_path / "kt", {"assets/gem/lang/en_us.json": '{"tooltip.gem.level.1": "One"}',
                                   "src/G.kt": 'val t = Text.translatable("tooltip.gem.level.$level")\n'})
    got = _by(langkeys.check(root))
    assert not any(kind == "missing_key" for kind, _ in got)
    assert ("unused_key", "tooltip.gem.level.1") not in got    # the template's head builds it


def test_placeholders_follow_what_the_game_loads():
    assert langkeys.placeholders("Speed %.1f") == {1: "s"}
    assert langkeys.placeholders("%2$5d of %1$d") == {1: "s", 2: "s"}


def test_numeric_placeholders_compare_as_the_game_sees_them(tmp_path):
    root = _tree(tmp_path / "ph", {
        "assets/gem/lang/en_us.json": '{"a.gem.x": "Speed %.1f", "a.gem.y": "Count %d", "a.gem.z": "%.1f blocks"}',
        "assets/gem/lang/de_de.json": '{"a.gem.x": "Tempo %s", "a.gem.y": "Anzahl %s", "a.gem.z": "Bloecke"}'})
    got = _by(langkeys.check(root, unused=False))
    assert {k for kind, k in got if kind == "placeholder_mismatch"} == {"a.gem.z"}


def test_every_default_file_of_a_namespace_is_the_default_locale(tmp_path):
    common = "common/src/main/resources/assets/gem/lang"
    fabric = "fabric/src/main/resources/assets/gem/lang"
    root = _tree(tmp_path / "ml", {f"{common}/en_us.json": '{"gem.common": "C"}',
                                   f"{fabric}/en_us.json": '{"gem.fabric": "F %s"}',
                                   f"{common}/de_de.json": '{"gem.common": "C", "gem.fabric": "F"}'})
    got = _by(langkeys.check(root, unused=False))
    assert ("extra_in_locale", "gem.fabric") not in got
    assert got[("placeholder_mismatch", "gem.fabric")]["other_at"] == f"{fabric}/en_us.json:1"
    root2 = _tree(tmp_path / "ml2", {f"{common}/en_us.json": '{"gem.common": "C"}',
                                     f"{fabric}/en_us.json": '{"gem.fabric": "F"}',
                                     f"{common}/de_de.json": '{"gem.common": "C"}'})
    miss = _by(langkeys.check(root2, unused=False))[("missing_in_locale", "gem.fabric")]
    assert miss["at"] == f"{fabric}/en_us.json:1" and miss["other_at"] == f"{common}/de_de.json"


def test_a_vanilla_override_does_not_make_vanilla_keys_missing(tmp_path):
    root = _tree(tmp_path / "van", {"assets/minecraft/lang/en_us.json": '{"item.minecraft.stick": "Twig"}',
                                    "assets/gem/lang/en_us.json": '{"gem.hello": "Hi"}',
                                    "src/C.java": 'class C { Object a = Text.translatable("item.minecraft.diamond"); }'})
    assert not any(f["kind"] == "missing_key" for f in langkeys.check(root, unused=False)["findings"])


def test_how_else_a_key_is_named(tmp_path):
    root = _tree(tmp_path / "use", {
        "assets/gem/lang/en_us.json": '{"gem.msg.hello": "Hi", "item.gem.tools.ruby_pick": "Pick"}',
        "assets/gem/lang/en_US.lang": "tile.gem.ruby_ore.name=Ruby Ore\nitemGroup.gem=Gems\n",
        "kubejs/client_scripts/a.js": "Text.translatable('gem.msg.hello')\n",
        "src/B.java": 'class B { void r() { block.setTranslationKey("gem.ruby_ore"); tab("gem"); '
                      'register("tools/ruby_pick"); } }'})
    assert not any(f["kind"] == "unused_key" for f in langkeys.check(root, "en_us")["findings"])


def test_findings_follow_the_file_line_by_line(tmp_path):
    keys = ",\n".join(f'  "gem.k{i}": "{i}"' for i in range(1, 12))
    root = _tree(tmp_path / "ord", {"assets/gem/lang/en_us.json": "{\n" + keys + "\n}",
                                    "assets/gem/lang/de_de.json": "{}"})
    lines = [int(f["at"].rpartition(":")[2]) for f in langkeys.check(root, unused=False)["findings"]]
    assert lines == sorted(lines) and lines[0] == 2


def test_no_file_of_the_default_locale_is_not_a_clean_pass(tmp_path, capsys):
    root = _tree(tmp_path / "typo", {"assets/gem/lang/en_us.json": '{"gem.a": "%s"}',
                                     "assets/gem/lang/de_de.json": '{"gem.a": ""}'})
    res = langkeys.lookup(root, "en_ux")
    assert res["status"] == "no_default" and not res["findings"]
    assert cli.main(["lang", "--repo", str(root), "--default", "en_ux"]) == 2
    assert "no namespace has a en_ux file" in capsys.readouterr().out


def test_a_value_the_game_cannot_read_fails_the_file(tmp_path):
    root = _tree(tmp_path / "bad", {"assets/gem/lang/en_us.json": '{"gem.a": "A",\n "gem.b": {"x": 1}}',
                                    "assets/gem/lang/de_de.json": '{"gem.a": 1, "gem.b": true}',
                                    "assets/deep/lang/en_us.json": '{"a":' + "[" * 100000 + "]" * 100000 + "}"})
    got = _by(langkeys.check(root, unused=False))
    bad = [f for f in langkeys.check(root, unused=False)["findings"] if f["kind"] == "invalid_file"]
    assert {f["at"] for f in bad} == {"assets/gem/lang/en_us.json:2", "assets/deep/lang/en_us.json:1"}
    assert ("missing_in_locale", "gem.a") not in got      # no default file read: nothing compared


def test_a_large_file_reads_in_linear_time(tmp_path):
    import time
    keys = ",\n".join(f'  "item.big.k{i}": "Value {i}"' for i in range(20000))
    root = _tree(tmp_path / "big", {"assets/big/lang/en_us.json": "{\n" + keys + "\n}"})
    t = time.perf_counter()
    lf = langkeys.read_lang(root, "assets/big/lang/en_us.json")
    assert time.perf_counter() - t < 3 and lf.line("item.big.k19999") == 20001


def test_a_bom_in_a_legacy_file_is_not_part_of_its_first_key(tmp_path):
    root = _tree(tmp_path / "bom", {"assets/old/lang/en_US.lang": b"\xef\xbb\xbfitem.old.x=X\n",
                                    "assets/old/lang/de_DE.lang": "item.old.x=Y\n"})
    assert langkeys.check(root, unused=False)["findings"] == []


def test_a_commented_out_call_asks_for_nothing(tmp_path):
    root = _tree(tmp_path / "cmt", {
        "assets/gem/lang/en_us.json": '{"gem.a": "A"}',
        "src/A.java": 'class A {\n  // Component.translatable("gem.removed_long_ago");\n'
                      '  /* Component.translatable("gem.gone"); */\n  String u = "http://x"; '
                      'Object b = Component.translatable("gem.real_missing");\n}\n'})
    got = [f for f in langkeys.check(root, unused=False)["findings"] if f["kind"] == "missing_key"]
    assert [(f["key"], f["at"]) for f in got] == [("gem.real_missing", "src/A.java:4")]
