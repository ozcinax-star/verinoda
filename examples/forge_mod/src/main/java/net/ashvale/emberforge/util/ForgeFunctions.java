package net.ashvale.emberforge.util;

import net.ashvale.emberforge.EmberForge;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.server.MinecraftServer;

/** Runs the mod's data-pack functions from code. */
public final class ForgeFunctions {
    public static final String RESET_FORGES = "emberforge:debug/reset_forges";

    private ForgeFunctions() {
    }

    /** Runs the reset function at the source's position; false when the data pack does not provide it. */
    public static boolean resetForges(CommandSourceStack source) {
        MinecraftServer server = source.getServer();
        ResourceLocation id = ResourceLocation.parse(RESET_FORGES);
        if (server.getFunctions().get(id).isEmpty()) {
            EmberForge.LOGGER.warn("Function {} is missing; is the mod's data pack disabled?", id);
            return false;
        }
        server.getCommands().performPrefixedCommand(source, "function " + RESET_FORGES);
        return true;
    }
}
