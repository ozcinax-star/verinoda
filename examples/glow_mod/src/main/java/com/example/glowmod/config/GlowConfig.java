package com.example.glowmod.config;

import com.example.glowmod.GlowMod;
import java.io.IOException;
import java.io.InputStream;
import java.io.Reader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Objects;
import net.fabricmc.loader.api.FabricLoader;
import org.yaml.snakeyaml.Yaml;

/** Settings from config/glowmod.yml; every missing key falls back to {@link #DEFAULTS}. */
public record GlowConfig(int ritualWispCount, double naturalChance, int blocksPerTick, int repairDelayTicks,
                         int maxSummonDistance) {
    private static final String FILE_NAME = "glowmod.yml";
    private static final GlowConfig DEFAULTS = new GlowConfig(4, 0.02, 8, 40, 8);
    private static volatile GlowConfig current = DEFAULTS;

    public static GlowConfig get() {
        return current;
    }

    /** Reads the file; on first start the commented default is copied out of the mod jar first. */
    public static void load() {
        Path file = FabricLoader.getInstance().getConfigDir().resolve(FILE_NAME);
        try {
            if (Files.notExists(file)) {
                try (InputStream in = GlowConfig.class.getResourceAsStream("/" + FILE_NAME)) {
                    Files.copy(Objects.requireNonNull(in, "default " + FILE_NAME + " missing from the jar"), file);
                }
            }
            try (Reader reader = Files.newBufferedReader(file)) {
                Map<String, Object> root = new Yaml().load(reader);
                current = parse(root == null ? Map.of() : root);
            }
        } catch (IOException | RuntimeException e) {
            GlowMod.LOGGER.error("could not read {}, using defaults", file, e);
            current = DEFAULTS;
        }
    }

    private static GlowConfig parse(Map<String, Object> root) {
        Map<String, Object> wisp = section(root, "wisp");
        Map<String, Object> repair = section(root, "repair");
        Map<String, Object> network = section(root, "network");
        return new GlowConfig(
                number(wisp, "ritual_count", DEFAULTS.ritualWispCount()).intValue(),
                number(wisp, "natural_chance", DEFAULTS.naturalChance()).doubleValue(),
                number(repair, "blocks_per_tick", DEFAULTS.blocksPerTick()).intValue(),
                number(repair, "delay_ticks", DEFAULTS.repairDelayTicks()).intValue(),
                number(network, "max_summon_distance", DEFAULTS.maxSummonDistance()).intValue());
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> section(Map<String, Object> root, String key) {
        return root.get(key) instanceof Map<?, ?> map ? (Map<String, Object>) map : Map.of();
    }

    private static Number number(Map<String, Object> section, String key, Number fallback) {
        return section.get(key) instanceof Number n ? n : fallback;
    }
}
