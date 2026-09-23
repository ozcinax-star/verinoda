package net.ashvale.emberforge.config;

import net.neoforged.neoforge.common.ModConfigSpec;

/**
 * Common settings. NeoForge writes them to config/emberforge-common.toml on first start;
 * the spec is registered in the EmberForge constructor.
 */
public final class EmberConfig {
    public static final ModConfigSpec.IntValue HEAT_PER_FUEL;
    public static final ModConfigSpec.IntValue MAX_HEAT;
    public static final ModConfigSpec.IntValue STOKE_HEAT;
    public static final ModConfigSpec.IntValue DECAY_INTERVAL;
    public static final ModConfigSpec.BooleanValue ALLOW_RESET_COMMAND;
    public static final ModConfigSpec SPEC;

    static {
        ModConfigSpec.Builder b = new ModConfigSpec.Builder();

        b.comment("Ember Forge heat settings").push("forge");
        HEAT_PER_FUEL = b.comment("Heat added by one fuel item (blaze rods count double)")
                .defineInRange("heat_per_fuel", 40, 1, 400);
        MAX_HEAT = b.comment("Upper limit of a forge's heat")
                .defineInRange("max_heat", 200, 50, 1000);
        STOKE_HEAT = b.comment("Heat added by the Stoke button or a Forge Hammer hit")
                .defineInRange("stoke_heat", 15, 0, 100);
        DECAY_INTERVAL = b.comment("Ticks between two losses of 1 heat")
                .defineInRange("decay_interval_ticks", 20, 1, 1200);
        b.pop();

        b.comment("Debug helpers").push("debug");
        ALLOW_RESET_COMMAND = b.comment("Allow operators to use /emberforge reset")
                .define("allow_reset_command", true);
        b.pop();

        SPEC = b.build();
    }

    private EmberConfig() {
    }
}
