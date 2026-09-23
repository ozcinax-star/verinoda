package com.example.glowmod.util;

import com.example.glowmod.GlowMod;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.Vec3d;

/** Runs .mcfunction files of the mod's data pack from Java. */
public final class Datapack {
    private Datapack() {
    }

    /** {@code name} is the function path without the namespace, e.g. "some_function". */
    public static void run(ServerWorld world, Vec3d pos, String name) {
        execute(world, pos, Identifier.of(GlowMod.MOD_ID, name));
    }

    /** {@code id} is a full function id, "namespace:path". */
    public static void runQualified(ServerWorld world, Vec3d pos, String id) {
        execute(world, pos, Identifier.of(id));
    }

    private static void execute(ServerWorld world, Vec3d pos, Identifier id) {
        MinecraftServer server = world.getServer();
        ServerCommandSource source = server.getCommandSource()
                .withWorld(world)
                .withPosition(pos)
                .withSilent()
                .withLevel(2);
        server.getCommandFunctionManager().getFunction(id).ifPresentOrElse(
                function -> server.getCommandFunctionManager().execute(function, source),
                () -> GlowMod.LOGGER.warn("data pack function {} not found", id));
    }
}
