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
    assert langkeys.placeholders("100%% sure, %d left") == {1: "d"}
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
