# Glow Mod (example)

A small, fictional Fabric mod for Minecraft 1.21.1, used as a public example and
benchmark corpus for Verinoda's support of mod repositories (Java code plus data
pack, assets and YAML configuration linked by resource ids). It has not been
compiled or run in a game; textures (`.png`) are left out on purpose, so the
item models point at files that do not exist here.

What it adds: a wisp mob, three items (lantern staff, ember shard, wisp heart),
a lantern ritual, the `/glow` operator command and one client key binding.

Layout:

- `src/main/java/com/example/glowmod/` - common (server + client) code
- `src/client/java/` - client-only code
- `src/gametest/java/` - Fabric game tests
- `src/main/resources/data/glowmod/` - data pack: functions, recipe, loot table
- `src/main/resources/assets/glowmod/` - item models and translations
- `src/main/resources/glowmod.yml` - default settings, copied to `config/` on first start
- `datapack/` - a standalone data pack for map makers (a copy of the wisp death function)
- `reference/original-plugin/` - excerpts of the Paper plugin this mod was ported
  from, kept for comparison; it is not built

Some identifiers are Turkish (`etkinlestir` = enable, `baslat` = start,
`kaydet` = record, `temizle` = clean up, `alaniAc` = clear the area), as in the
private mod this example imitates.
