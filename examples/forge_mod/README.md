# Emberforge (example mod)

A small, fictional NeoForge 1.21.1 mod used as a test corpus for Verinoda. It builds on
paper only: textures (`.png`) are left out so the example stays plain text, and nothing
here has been run in a game.

Adds:

- **Ember Forge** - a block that burns fuel into heat and slowly forges its input with
  `emberforge:forging` recipes. Right-click opens it; a Forge Hammer stokes it.
- **Cinder Ore** - generated in a few overworld biomes; forged into two Cinder Ingots
  (a furnace gives one).
- `/emberforge reset` and `/emberforge heat <pos>` for operators.

Layout:

- `src/main/java` - Java code (registries, block, block entity, menu, recipe, network, events)
- `src/main/kotlin` - Kotlin code (heat rules, forge screen, commands); needs Kotlin for Forge
- `src/main/resources/data/emberforge` - recipes, tags, loot tables, worldgen, advancement, functions
- `src/main/resources/assets/emberforge` - blockstates, models, `en_us` / `tr_tr` translations
- `config/emberforge-common.toml` - the default common config, for reference
- `src/test/java` - JUnit tests
